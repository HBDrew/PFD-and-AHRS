// SPDX-License-Identifier: GPL-2.0
/*
 * ws35e_panel.c – Waveshare 3.5" DSI (E) panel driver
 *
 * Controller : ST7701S (2-lane MIPI DSI, 640×480 @ 60 Hz)
 * Compatible : "waveshare,ws35dsi-e"
 *
 * Build  : make
 * Load   : sudo insmod ws35e_panel.ko
 * Verify : dmesg | grep ws35e
 *
 * DCS init is deferred to enable() so the vc4 DSI DPHY/PLL
 * is fully active before any lane traffic is attempted.
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
};

static inline struct ws35e *to_ws35e(struct drm_panel *panel)
{
	return container_of(panel, struct ws35e, panel);
}

static int ws_write(struct mipi_dsi_device *dsi, const u8 *buf, size_t len)
{
	ssize_t ret = mipi_dsi_dcs_write_buffer(dsi, buf, len);
	if (ret < 0) {
		dev_warn(&dsi->dev, "DSI write failed (%zd)\n", ret);
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
 * ST7701S initialisation – called from enable() after the vc4 DSI
 * encoder has set up the DPHY and started the pixel clock.
 */
static int ws35e_init(struct ws35e *ctx)
{
	struct mipi_dsi_device *dsi = ctx->dsi;
	int ret = 0;

	/* ── CMD2 BK0 – basic display config ─────────────────────── */
	ret |= WS_SEQ(dsi, 0xFF, 0x77, 0x01, 0x00, 0x00, 0x10);
	ret |= WS_SEQ(dsi, 0xC0, 0x3B, 0x00);   /* LNESET  480 lines   */
	ret |= WS_SEQ(dsi, 0xC1, 0x0D, 0x02);   /* PORCTRL VBP/VFP     */
	ret |= WS_SEQ(dsi, 0xC2, 0x21, 0x08);   /* INVSEL  col-inv 60Hz*/
	ret |= WS_SEQ(dsi, 0xCC, 0x10);         /* GPC     RGB order   */

	/* Positive gamma */
	ret |= WS_SEQ(dsi, 0xB0,
		0x00, 0x0E, 0x15, 0x0F, 0x11, 0x08,
		0x08, 0x08, 0x08, 0x23, 0x04, 0x13,
		0x12, 0x2B, 0x34, 0x1F);

	/* Negative gamma */
	ret |= WS_SEQ(dsi, 0xB1,
		0x00, 0x0E, 0x15, 0x0F, 0x11, 0x08,
		0x08, 0x08, 0x08, 0x23, 0x04, 0x13,
		0x12, 0x2B, 0x34, 0x1F);

	/* ── CMD2 BK1 – power / MIPI settings ────────────────────── */
	ret |= WS_SEQ(dsi, 0xFF, 0x77, 0x01, 0x00, 0x00, 0x11);
	ret |= WS_SEQ(dsi, 0xB0, 0x4D);   /* VRHS      */
	ret |= WS_SEQ(dsi, 0xB1, 0x2B);   /* VCOMS     */
	ret |= WS_SEQ(dsi, 0xB2, 0x07);   /* VGHSS     */
	ret |= WS_SEQ(dsi, 0xB3, 0x80);   /* TESTCMD   */
	ret |= WS_SEQ(dsi, 0xB5, 0x47);   /* VGLS      */
	ret |= WS_SEQ(dsi, 0xB7, 0x85);   /* PWCTLR1   */
	ret |= WS_SEQ(dsi, 0xB8, 0x21);   /* PWCTLR2   */
	ret |= WS_SEQ(dsi, 0xC1, 0x78);   /* SPD1      */
	ret |= WS_SEQ(dsi, 0xC2, 0x78);   /* SPD2      */
	ret |= WS_SEQ(dsi, 0xD0, 0x88);   /* MIPISET1  */

	msleep(100);

	ret |= WS_SEQ(dsi, 0xE0, 0x00, 0x00, 0x02);
	ret |= WS_SEQ(dsi, 0xE1,
		0x06, 0x00, 0x08, 0x00,
		0x05, 0x00, 0x07, 0x00,
		0x00, 0x33, 0x33);
	ret |= WS_SEQ(dsi, 0xE2,
		0x30, 0x30, 0x33, 0x33,
		0x34, 0x00, 0x00, 0x00,
		0x34, 0x00, 0x00, 0x00);
	ret |= WS_SEQ(dsi, 0xE3, 0x00, 0x00, 0x33, 0x33);
	ret |= WS_SEQ(dsi, 0xE4, 0x44, 0x44);
	ret |= WS_SEQ(dsi, 0xE5,
		0x0D, 0x31, 0xC8, 0xAF,
		0x0F, 0x33, 0xC8, 0xAF,
		0x09, 0x2D, 0xC8, 0xAF,
		0x0B, 0x2F, 0xC8, 0xAF);
	ret |= WS_SEQ(dsi, 0xE6, 0x00, 0x00, 0x33, 0x33);
	ret |= WS_SEQ(dsi, 0xE7, 0x44, 0x44);
	ret |= WS_SEQ(dsi, 0xE8,
		0x0E, 0x32, 0xC8, 0xAF,
		0x10, 0x34, 0xC8, 0xAF,
		0x0A, 0x2E, 0xC8, 0xAF,
		0x0C, 0x30, 0xC8, 0xAF);
	ret |= WS_SEQ(dsi, 0xEB, 0x02, 0x00, 0xE4, 0xE4, 0x44, 0x00, 0x40);
	ret |= WS_SEQ(dsi, 0xEC, 0x3C, 0x00);
	ret |= WS_SEQ(dsi, 0xED,
		0xAB, 0x89, 0x76, 0x54,
		0x01, 0xFF, 0xFF, 0x10,
		0xFF, 0xFF, 0xFF, 0x10,
		0x45, 0x67, 0x98, 0xBA);

	/* ── Return to CMD1 ───────────────────────────────────────── */
	ret |= WS_SEQ(dsi, 0xFF, 0x77, 0x01, 0x00, 0x00, 0x00);

	if (ret) {
		dev_err(&dsi->dev, "ws35e: DCS write error during init\n");
		return ret;
	}

	ret = mipi_dsi_dcs_exit_sleep_mode(dsi);
	if (ret < 0)
		return ret;
	msleep(120);

	ret = mipi_dsi_dcs_set_display_on(dsi);
	if (ret < 0)
		return ret;
	msleep(20);

	dev_info(&dsi->dev, "ws35e: ST7701S init complete\n");
	return 0;
}

/* ── drm_panel callbacks ──────────────────────────────────────────── */

/*
 * prepare() – power rails + hardware reset only.
 * No DCS here: the vc4 DPHY isn't running yet at this point.
 */
static int ws35e_prepare(struct drm_panel *panel)
{
	struct ws35e *ctx = to_ws35e(panel);

	if (ctx->prepared)
		return 0;

	if (ctx->reset) {
		/* Assert reset, then DEASSERT and leave deasserted */
		gpiod_set_value_cansleep(ctx->reset, 1);  /* assert   */
		msleep(10);
		gpiod_set_value_cansleep(ctx->reset, 0);  /* deassert */
		msleep(120); /* panel startup; stays deasserted into enable() */
	} else {
		msleep(50);
	}

	ctx->prepared = true;
	dev_dbg(&ctx->dsi->dev, "ws35e: prepared (DPHY not yet active)\n");
	return 0;
}

/*
 * enable() – called after vc4_dsi encoder_enable(), so HS clock is live.
 * Send the full ST7701S init sequence here.
 */
static int ws35e_enable(struct drm_panel *panel)
{
	struct ws35e *ctx = to_ws35e(panel);
	int ret;

	if (ctx->enabled)
		return 0;

	ret = ws35e_init(ctx);
	if (ret) {
		dev_err(&ctx->dsi->dev,
			"ws35e: panel init failed: %d\n", ret);
		return ret;
	}

	ctx->enabled = true;
	return 0;
}

static int ws35e_disable(struct drm_panel *panel)
{
	struct ws35e *ctx = to_ws35e(panel);

	if (!ctx->enabled)
		return 0;

	mipi_dsi_dcs_set_display_off(ctx->dsi);
	msleep(20);
	ctx->enabled = false;
	return 0;
}

static int ws35e_unprepare(struct drm_panel *panel)
{
	struct ws35e *ctx = to_ws35e(panel);

	if (!ctx->prepared)
		return 0;

	mipi_dsi_dcs_enter_sleep_mode(ctx->dsi);
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

	dsi->lanes      = 2;
	dsi->format     = MIPI_DSI_FMT_RGB888;
	/*
	 * VIDEO_BURST + LPM: without NON_CONTINUOUS the byte-clock stays up
	 * (stat bit 11 = TX done confirmed), LP escape commands are interleaved
	 * by the vc4 driver during blanking.  NON_CONTINUOUS broke TX completion.
	 */
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
MODULE_DESCRIPTION("Waveshare 3.5-inch DSI (E) — ST7701S, init deferred to enable()");
MODULE_LICENSE("GPL");
