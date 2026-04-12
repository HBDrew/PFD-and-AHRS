// SPDX-License-Identifier: GPL-2.0
/*
 * ws35e_panel.c – Waveshare 3.5" DSI (E) panel driver
 *
 * Controller : ST7701S (1-lane MIPI DSI, 640×480 @ 60 Hz)
 * Compatible : "waveshare,ws35dsi-e"
 *
 * Build  : make
 * Load   : sudo insmod ws35e_panel.ko
 * Verify : dmesg | grep ws35e
 *
 * Init strategy:
 *   1. prepare() – LP mode init attempt (before video starts, no BTA conflict)
 *   2. enable()  – retry if prepare() failed (DPHY active, video streaming)
 *
 * All DCS writes use MIPI_DSI_MSG_USE_LPM via host->ops->transfer() to avoid
 * vc4_dsi silently overriding LP to HS during active video mode.
 *
 * init_done flag prevents double-init; sleep_out/display_on are always
 * attempted in ws35e_init() so we can observe whether basic DCS works
 * even when CMD2 page commands fail.
 */

#include <linux/delay.h>
#include <linux/gpio/consumer.h>
#include <linux/module.h>
#include <linux/of.h>
#include <drm/drm_mipi_dsi.h>
#include <drm/drm_modes.h>
#include <drm/drm_panel.h>
#include <video/mipi_display.h>

struct ws35e {
	struct drm_panel panel;
	struct mipi_dsi_device *dsi;
	struct gpio_desc *reset;
	bool prepared;
	bool enabled;
	bool init_done;  /* set when sleep_out + display_on succeed */
};

static inline struct ws35e *to_ws35e(struct drm_panel *panel)
{
	return container_of(panel, struct ws35e, panel);
}

/*
 * ws_write – send one DCS command in LP mode.
 *
 * Calls host->ops->transfer() directly with MIPI_DSI_MSG_USE_LPM so the LP
 * flag cannot be stripped by vc4_dsi's video-mode HS override path.
 */
static int ws_write(struct mipi_dsi_device *dsi, const u8 *buf, size_t len)
{
	struct mipi_dsi_msg msg;
	ssize_t ret;
	u8 type;

	switch (len) {
	case 0:  return -EINVAL;
	case 1:  type = MIPI_DSI_DCS_SHORT_WRITE; break;
	case 2:  type = MIPI_DSI_DCS_SHORT_WRITE_PARAM; break;
	default: type = MIPI_DSI_DCS_LONG_WRITE; break;
	}

	memset(&msg, 0, sizeof(msg));
	msg.channel = dsi->channel;
	msg.type    = type;
	msg.flags   = MIPI_DSI_MSG_USE_LPM;
	msg.tx_buf  = buf;
	msg.tx_len  = len;

	ret = dsi->host->ops->transfer(dsi->host, &msg);
	if (ret < 0) {
		dev_warn(&dsi->dev, "DSI LP write failed (%zd)\n", ret);
		return (int)ret;
	}
	return 0;
}

#define WS_SEQ(dsi, ...) \
({ \
	const u8 __d[] = { __VA_ARGS__ }; \
	ws_write((dsi), __d, ARRAY_SIZE(__d)); \
})

/*
 * ws35e_init – ST7701S full init sequence.
 *
 * CMD2 write failures are logged but do not abort – we always attempt
 * sleep_out and display_on so we can observe whether basic DCS reaches
 * the panel even when manufacturer commands fail.
 *
 * Returns 0 if sleep_out AND display_on succeed (init_done condition).
 * Returns -errno if even the basic DCS commands cannot be sent.
 */
static int ws35e_init(struct ws35e *ctx)
{
	struct mipi_dsi_device *dsi = ctx->dsi;
	int cmd2_err = 0;
	int ret;
	u8 cmd;

	/* ── CMD2 BK0 – basic display config ─────────────────────── */
	cmd2_err |= WS_SEQ(dsi, 0xFF, 0x77, 0x01, 0x00, 0x00, 0x10);
	cmd2_err |= WS_SEQ(dsi, 0xC0, 0x3B, 0x00);   /* LNESET  480 lines   */
	cmd2_err |= WS_SEQ(dsi, 0xC1, 0x0D, 0x02);   /* PORCTRL VBP/VFP     */
	cmd2_err |= WS_SEQ(dsi, 0xC2, 0x21, 0x08);   /* INVSEL  col-inv 60Hz*/
	cmd2_err |= WS_SEQ(dsi, 0xCC, 0x10);         /* GPC     RGB order   */

	/* Positive gamma */
	cmd2_err |= WS_SEQ(dsi, 0xB0,
		0x00, 0x0E, 0x15, 0x0F, 0x11, 0x08,
		0x08, 0x08, 0x08, 0x23, 0x04, 0x13,
		0x12, 0x2B, 0x34, 0x1F);

	/* Negative gamma */
	cmd2_err |= WS_SEQ(dsi, 0xB1,
		0x00, 0x0E, 0x15, 0x0F, 0x11, 0x08,
		0x08, 0x08, 0x08, 0x23, 0x04, 0x13,
		0x12, 0x2B, 0x34, 0x1F);

	/* ── CMD2 BK1 – power / MIPI settings ────────────────────── */
	cmd2_err |= WS_SEQ(dsi, 0xFF, 0x77, 0x01, 0x00, 0x00, 0x11);
	cmd2_err |= WS_SEQ(dsi, 0xB0, 0x4D);   /* VRHS      */
	cmd2_err |= WS_SEQ(dsi, 0xB1, 0x2B);   /* VCOMS     */
	cmd2_err |= WS_SEQ(dsi, 0xB2, 0x07);   /* VGHSS     */
	cmd2_err |= WS_SEQ(dsi, 0xB3, 0x80);   /* TESTCMD   */
	cmd2_err |= WS_SEQ(dsi, 0xB5, 0x47);   /* VGLS      */
	cmd2_err |= WS_SEQ(dsi, 0xB7, 0x85);   /* PWCTLR1   */
	cmd2_err |= WS_SEQ(dsi, 0xB8, 0x21);   /* PWCTLR2   */
	cmd2_err |= WS_SEQ(dsi, 0xC1, 0x78);   /* SPD1      */
	cmd2_err |= WS_SEQ(dsi, 0xC2, 0x78);   /* SPD2      */
	cmd2_err |= WS_SEQ(dsi, 0xD0, 0x88);   /* MIPISET1  */

	msleep(100);

	cmd2_err |= WS_SEQ(dsi, 0xE0, 0x00, 0x00, 0x02);
	cmd2_err |= WS_SEQ(dsi, 0xE1,
		0x06, 0x00, 0x08, 0x00,
		0x05, 0x00, 0x07, 0x00,
		0x00, 0x33, 0x33);
	cmd2_err |= WS_SEQ(dsi, 0xE2,
		0x30, 0x30, 0x33, 0x33,
		0x34, 0x00, 0x00, 0x00,
		0x34, 0x00, 0x00, 0x00);
	cmd2_err |= WS_SEQ(dsi, 0xE3, 0x00, 0x00, 0x33, 0x33);
	cmd2_err |= WS_SEQ(dsi, 0xE4, 0x44, 0x44);
	cmd2_err |= WS_SEQ(dsi, 0xE5,
		0x0D, 0x31, 0xC8, 0xAF,
		0x0F, 0x33, 0xC8, 0xAF,
		0x09, 0x2D, 0xC8, 0xAF,
		0x0B, 0x2F, 0xC8, 0xAF);
	cmd2_err |= WS_SEQ(dsi, 0xE6, 0x00, 0x00, 0x33, 0x33);
	cmd2_err |= WS_SEQ(dsi, 0xE7, 0x44, 0x44);
	cmd2_err |= WS_SEQ(dsi, 0xE8,
		0x0E, 0x32, 0xC8, 0xAF,
		0x10, 0x34, 0xC8, 0xAF,
		0x0A, 0x2E, 0xC8, 0xAF,
		0x0C, 0x30, 0xC8, 0xAF);
	cmd2_err |= WS_SEQ(dsi, 0xEB, 0x02, 0x00, 0xE4, 0xE4, 0x44, 0x00, 0x40);
	cmd2_err |= WS_SEQ(dsi, 0xEC, 0x3C, 0x00);
	cmd2_err |= WS_SEQ(dsi, 0xED,
		0xAB, 0x89, 0x76, 0x54,
		0x01, 0xFF, 0xFF, 0x10,
		0xFF, 0xFF, 0xFF, 0x10,
		0x45, 0x67, 0x98, 0xBA);

	/* ── Return to CMD1 ───────────────────────────────────────── */
	cmd2_err |= WS_SEQ(dsi, 0xFF, 0x77, 0x01, 0x00, 0x00, 0x00);

	if (cmd2_err)
		dev_warn(&dsi->dev,
			"ws35e: CMD2 writes had errors – gamma/power not set\n");

	/*
	 * Always send sleep_out and display_on regardless of CMD2 result.
	 * These are standard DCS commands that must work for any display output.
	 * Return value here determines ctx->init_done.
	 */
	cmd = MIPI_DCS_EXIT_SLEEP_MODE;
	ret = ws_write(dsi, &cmd, 1);
	if (ret) {
		dev_warn(&dsi->dev, "ws35e: sleep_out failed: %d\n", ret);
		return ret;  /* DSI can't reach panel at all */
	}
	msleep(120);

	cmd = MIPI_DCS_SET_DISPLAY_ON;
	ret = ws_write(dsi, &cmd, 1);
	if (ret) {
		dev_warn(&dsi->dev, "ws35e: display_on failed: %d\n", ret);
		return ret;
	}
	msleep(20);

	dev_info(&dsi->dev,
		"ws35e: ST7701S init complete (CMD2 errors: %d)\n", cmd2_err);
	return 0;
}

/* ── drm_panel callbacks ──────────────────────────────────────────── */

/*
 * prepare() – hardware reset, then attempt LP DCS init.
 *
 * LP mode (MIPI_DSI_MSG_USE_LPM) does not require the DPHY HS clock –
 * only the DSI escape clock, which is enabled in vc4_dsi's probe/attach.
 * If LP init succeeds here (before video starts), we avoid the BTA timeout
 * that occurs when injecting DCS into an active HS video stream.
 *
 * On failure prepare() still returns 0; enable() will retry.
 */
static int ws35e_prepare(struct drm_panel *panel)
{
	struct ws35e *ctx = to_ws35e(panel);

	if (ctx->prepared)
		return 0;

	if (ctx->reset) {
		gpiod_set_value_cansleep(ctx->reset, 1);  /* assert   */
		msleep(10);
		gpiod_set_value_cansleep(ctx->reset, 0);  /* deassert */
		msleep(120);
	} else {
		msleep(120);  /* wait for panel VCC stabilisation */
	}

	dev_info(&ctx->dsi->dev, "ws35e: prepare – attempting LP DCS init\n");

	if (!ws35e_init(ctx)) {
		ctx->init_done = true;
		dev_info(&ctx->dsi->dev,
			"ws35e: LP init in prepare() SUCCEEDED – panel ready\n");
	} else {
		dev_warn(&ctx->dsi->dev,
			"ws35e: LP init in prepare() failed; will retry in enable()\n");
	}

	ctx->prepared = true;
	return 0;
}

/*
 * enable() – called after vc4_dsi encoder_enable() (HS clock live, video streaming).
 *
 * Only runs init if prepare() did not succeed.  If init is still needed here,
 * DCS commands contend with the active video stream – LP-to-HS override may
 * occur.  We log the outcome and always mark enabled so DRM doesn't loop.
 */
static int ws35e_enable(struct drm_panel *panel)
{
	struct ws35e *ctx = to_ws35e(panel);

	if (ctx->enabled)
		return 0;

	if (!ctx->init_done) {
		dev_info(&ctx->dsi->dev,
			"ws35e: enable – prepare() init failed; retrying with video active\n");
		if (!ws35e_init(ctx))
			ctx->init_done = true;
		else
			dev_warn(&ctx->dsi->dev,
				"ws35e: enable-time init also failed – panel may be blank\n");
	}

	ctx->enabled = true;
	return 0;
}

static int ws35e_disable(struct drm_panel *panel)
{
	struct ws35e *ctx = to_ws35e(panel);
	u8 cmd;

	if (!ctx->enabled)
		return 0;

	cmd = MIPI_DCS_SET_DISPLAY_OFF;
	ws_write(ctx->dsi, &cmd, 1);
	msleep(20);
	ctx->enabled = false;
	ctx->init_done = false;
	return 0;
}

static int ws35e_unprepare(struct drm_panel *panel)
{
	struct ws35e *ctx = to_ws35e(panel);
	u8 cmd;

	if (!ctx->prepared)
		return 0;

	cmd = MIPI_DCS_ENTER_SLEEP_MODE;
	ws_write(ctx->dsi, &cmd, 1);
	msleep(100);

	if (ctx->reset)
		gpiod_set_value_cansleep(ctx->reset, 0);

	ctx->prepared = false;
	return 0;
}

static int ws35e_get_modes(struct drm_panel *panel,
			   struct drm_connector *connector)
{
	struct drm_display_mode *mode;

	mode = drm_mode_create(connector->dev);
	if (!mode)
		return -ENOMEM;

	mode->clock       = 24000;
	mode->hdisplay    = 640;
	mode->hsync_start = 640 + 48;
	mode->hsync_end   = 640 + 48 + 32;
	mode->htotal      = 640 + 48 + 32 + 80;
	mode->vdisplay    = 480;
	mode->vsync_start = 480 + 3;
	mode->vsync_end   = 480 + 3 + 4;
	mode->vtotal      = 480 + 3 + 4 + 13;
	mode->type        = DRM_MODE_TYPE_DRIVER | DRM_MODE_TYPE_PREFERRED;

	drm_mode_set_name(mode);
	drm_mode_probed_add(connector, mode);

	connector->display_info.width_mm  = 70;
	connector->display_info.height_mm = 50;

	return 1;
}

static const struct drm_panel_funcs ws35e_funcs = {
	.prepare   = ws35e_prepare,
	.enable    = ws35e_enable,
	.disable   = ws35e_disable,
	.unprepare = ws35e_unprepare,
	.get_modes = ws35e_get_modes,
};

/* ── MIPI DSI driver ──────────────────────────────────────────────── */

static int ws35e_probe(struct mipi_dsi_device *dsi)
{
	struct device *dev = &dsi->dev;
	struct ws35e *ctx;
	int ret;

	ctx = devm_kzalloc(dev, sizeof(*ctx), GFP_KERNEL);
	if (!ctx)
		return -ENOMEM;

	ctx->reset = devm_gpiod_get_optional(dev, "reset", GPIOD_OUT_LOW);
	if (IS_ERR(ctx->reset))
		return PTR_ERR(ctx->reset);

	ctx->dsi = dsi;
	mipi_dsi_set_drvdata(dsi, ctx);

	dsi->lanes      = 1;
	dsi->format     = MIPI_DSI_FMT_RGB888;
	dsi->mode_flags = MIPI_DSI_MODE_VIDEO |
			  MIPI_DSI_MODE_VIDEO_BURST |
			  MIPI_DSI_MODE_LPM;

	drm_panel_init(&ctx->panel, dev, &ws35e_funcs,
		       DRM_MODE_CONNECTOR_DSI);

	ret = drm_panel_of_backlight(&ctx->panel);
	if (ret)
		return ret;

	drm_panel_add(&ctx->panel);

	ret = mipi_dsi_attach(dsi);
	if (ret < 0) {
		dev_err(dev, "failed to attach DSI: %d\n", ret);
		drm_panel_remove(&ctx->panel);
		return ret;
	}

	dev_info(dev, "ws35e panel attached\n");
	return 0;
}

static void ws35e_remove(struct mipi_dsi_device *dsi)
{
	struct ws35e *ctx = mipi_dsi_get_drvdata(dsi);

	mipi_dsi_detach(dsi);
	drm_panel_remove(&ctx->panel);
}

static const struct of_device_id ws35e_of_match[] = {
	{ .compatible = "waveshare,ws35dsi-e" },
	{ }
};
MODULE_DEVICE_TABLE(of, ws35e_of_match);

static struct mipi_dsi_driver ws35e_driver = {
	.probe  = ws35e_probe,
	.remove = ws35e_remove,
	.driver = {
		.name           = "ws35e-panel",
		.of_match_table = ws35e_of_match,
	},
};
module_mipi_dsi_driver(ws35e_driver);

MODULE_AUTHOR("PFD-AHRS Project");
MODULE_DESCRIPTION("Waveshare 3.5-inch DSI (E) — ST7701S, LP init attempted in prepare()");
MODULE_LICENSE("GPL");
