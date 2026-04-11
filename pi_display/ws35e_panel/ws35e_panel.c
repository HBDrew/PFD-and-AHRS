// SPDX-License-Identifier: GPL-2.0
/*
 * ws35e_panel.c – Waveshare 3.5" DSI (E) panel driver
 *
 * Controller : ST7701S (2-lane MIPI DSI, 640×480 @ 60 Hz)
 * Compatible : "waveshare,ws35dsi-e"
 *
 * Kernel headers: /usr/src/linux-headers-$(uname -r)
 * Build        : make  (see Makefile)
 * Install      : sudo insmod ws35e_panel.ko
 *
 * The Waveshare overlay (Waveshare_35DSI.dtbo) uses
 * compatible = "Generic,panel-dsi" which binds panel-simple-dsi.
 * That driver sends only sleep_out + display_on — no ST7701S init.
 *
 * To use this driver:
 *   1. Decompile the overlay, change compatible to "waveshare,ws35dsi-e"
 *   2. Recompile and place back in /boot/firmware/overlays/
 *   3. sudo make && sudo insmod ws35e_panel.ko
 *   4. Reboot  (or: echo waveshare,ws35dsi-e > bind path if hotplug works)
 *
 * Alternatively if panel-simple is a loadable module:
 *   sudo modprobe -r panel-simple
 *   Change compatible in this file to "Generic,panel-dsi"
 *   sudo insmod ws35e_panel.ko
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
};

static inline struct ws35e *to_ws35e(struct drm_panel *panel)
{
	return container_of(panel, struct ws35e, panel);
}

/* Write a raw byte sequence as a DCS command */
static int ws_write(struct mipi_dsi_device *dsi, const u8 *buf, size_t len)
{
	ssize_t ret = mipi_dsi_dcs_write_buffer(dsi, buf, len);
	if (ret < 0)
		return (int)ret;
	return 0;
}

#define WS_SEQ(dsi, ...) \
({ \
	const u8 __d[] = { __VA_ARGS__ }; \
	ws_write((dsi), __d, ARRAY_SIZE(__d)); \
})

/*
 * ST7701S initialisation sequence for 640×480, 2-lane, 24 MHz pixel clock.
 *
 * NOTE: gamma register values below are reasonable defaults.
 * If the panel shows wrong colours after init, the gamma tables
 * (B0/B1 under BK0, and E0-EC under BK1) may need tuning.
 */
static int ws35e_init(struct ws35e *ctx)
{
	struct mipi_dsi_device *dsi = ctx->dsi;
	int ret = 0;

	/* Allow panel power rails and oscillator to settle */
	msleep(20);

	/* ── CMD2 BK0 – basic display config ─────────────────────── */
	ret |= WS_SEQ(dsi, 0xFF, 0x77, 0x01, 0x00, 0x00, 0x10);

	/* LNESET: 480 active lines (0x3C → (0x3C+1)×8 = 496; use 0x3B for 480) */
	ret |= WS_SEQ(dsi, 0xC0, 0x3B, 0x00);

	/* PORCTRL: VBP=13, VFP=3 */
	ret |= WS_SEQ(dsi, 0xC1, 0x0D, 0x02);

	/* INVSEL: column inversion, 60 Hz frame rate */
	ret |= WS_SEQ(dsi, 0xC2, 0x21, 0x08);

	/* GPC: RGB order */
	ret |= WS_SEQ(dsi, 0xCC, 0x10);

	/* Positive gamma (B0) */
	ret |= WS_SEQ(dsi, 0xB0,
		0x00, 0x0E, 0x15, 0x0F, 0x11, 0x08,
		0x08, 0x08, 0x08, 0x23, 0x04, 0x13,
		0x12, 0x2B, 0x34, 0x1F);

	/* Negative gamma (B1) */
	ret |= WS_SEQ(dsi, 0xB1,
		0x00, 0x0E, 0x15, 0x0F, 0x11, 0x08,
		0x08, 0x08, 0x08, 0x23, 0x04, 0x13,
		0x12, 0x2B, 0x34, 0x1F);

	/* ── CMD2 BK1 – power / MIPI settings ────────────────────── */
	ret |= WS_SEQ(dsi, 0xFF, 0x77, 0x01, 0x00, 0x00, 0x11);

	ret |= WS_SEQ(dsi, 0xB0, 0x4D);   /* VRHS  */
	ret |= WS_SEQ(dsi, 0xB1, 0x2B);   /* VCOMS */
	ret |= WS_SEQ(dsi, 0xB2, 0x07);   /* VGHSS */
	ret |= WS_SEQ(dsi, 0xB3, 0x80);   /* TESTCMD */
	ret |= WS_SEQ(dsi, 0xB5, 0x47);   /* VGLS  */
	ret |= WS_SEQ(dsi, 0xB7, 0x85);   /* PWCTLR1 */
	ret |= WS_SEQ(dsi, 0xB8, 0x21);   /* PWCTLR2 */
	ret |= WS_SEQ(dsi, 0xC1, 0x78);   /* SPD1   */
	ret |= WS_SEQ(dsi, 0xC2, 0x78);   /* SPD2   */
	ret |= WS_SEQ(dsi, 0xD0, 0x88);   /* MIPISET1 */

	msleep(100);

	/* Gate timing control */
	ret |= WS_SEQ(dsi, 0xE0, 0x00, 0x00, 0x02);

	/* Source timing 1 */
	ret |= WS_SEQ(dsi, 0xE1,
		0x06, 0x00, 0x08, 0x00,
		0x05, 0x00, 0x07, 0x00,
		0x00, 0x33, 0x33);

	/* Source timing 2 */
	ret |= WS_SEQ(dsi, 0xE2,
		0x30, 0x30, 0x33, 0x33,
		0x34, 0x00, 0x00, 0x00,
		0x34, 0x00, 0x00, 0x00);

	/* Gate EQ */
	ret |= WS_SEQ(dsi, 0xE3, 0x00, 0x00, 0x33, 0x33);
	ret |= WS_SEQ(dsi, 0xE4, 0x44, 0x44);

	/* Source EQ 1 */
	ret |= WS_SEQ(dsi, 0xE5,
		0x0D, 0x31, 0xC8, 0xAF,
		0x0F, 0x33, 0xC8, 0xAF,
		0x09, 0x2D, 0xC8, 0xAF,
		0x0B, 0x2F, 0xC8, 0xAF);

	ret |= WS_SEQ(dsi, 0xE6, 0x00, 0x00, 0x33, 0x33);
	ret |= WS_SEQ(dsi, 0xE7, 0x44, 0x44);

	/* Source EQ 2 */
	ret |= WS_SEQ(dsi, 0xE8,
		0x0E, 0x32, 0xC8, 0xAF,
		0x10, 0x34, 0xC8, 0xAF,
		0x0A, 0x2E, 0xC8, 0xAF,
		0x0C, 0x30, 0xC8, 0xAF);

	/* Digital GMA */
	ret |= WS_SEQ(dsi, 0xEB,
		0x02, 0x00, 0xE4, 0xE4,
		0x44, 0x00, 0x40);

	ret |= WS_SEQ(dsi, 0xEC, 0x3C, 0x00);

	/* Source output select */
	ret |= WS_SEQ(dsi, 0xED,
		0xAB, 0x89, 0x76, 0x54,
		0x01, 0xFF, 0xFF, 0x10,
		0xFF, 0xFF, 0xFF, 0x10,
		0x45, 0x67, 0x98, 0xBA);

	/* ── Return to CMD1 ───────────────────────────────────────── */
	ret |= WS_SEQ(dsi, 0xFF, 0x77, 0x01, 0x00, 0x00, 0x00);

	if (ret) {
		dev_err(&dsi->dev, "ws35e: DSI write error during init\n");
		return ret;
	}

	/* Sleep-out then display-on */
	ret = mipi_dsi_dcs_exit_sleep_mode(dsi);
	if (ret < 0) {
		dev_err(&dsi->dev, "ws35e: sleep_out failed: %d\n", ret);
		return ret;
	}
	msleep(120);

	ret = mipi_dsi_dcs_set_display_on(dsi);
	if (ret < 0) {
		dev_err(&dsi->dev, "ws35e: display_on failed: %d\n", ret);
		return ret;
	}
	msleep(20);

	return 0;
}

/* ── drm_panel callbacks ──────────────────────────────────────────────────── */

static int ws35e_prepare(struct drm_panel *panel)
{
	struct ws35e *ctx = to_ws35e(panel);

	if (ctx->prepared)
		return 0;

	if (ctx->reset) {
		gpiod_set_value_cansleep(ctx->reset, 1);
		msleep(10);
		gpiod_set_value_cansleep(ctx->reset, 0);
		msleep(20);
		gpiod_set_value_cansleep(ctx->reset, 1);
		msleep(50);
	}

	if (ws35e_init(ctx))
		return -EIO;

	ctx->prepared = true;
	return 0;
}

static int ws35e_unprepare(struct drm_panel *panel)
{
	struct ws35e *ctx = to_ws35e(panel);

	if (!ctx->prepared)
		return 0;

	mipi_dsi_dcs_set_display_off(ctx->dsi);
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

	/*
	 * Timing taken from Waveshare_35DSI.dtbo:
	 *   hactive=640  hbp=80  hfp=48  hsync=32
	 *   vactive=480  vbp=13  vfp=3   vsync=4
	 *   pixelclk=24 MHz
	 */
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
	.unprepare = ws35e_unprepare,
	.get_modes = ws35e_get_modes,
};

/* ── MIPI DSI driver ────────────────────────────────────────────────────── */

static int ws35e_probe(struct mipi_dsi_device *dsi)
{
	struct device *dev = &dsi->dev;
	struct ws35e *ctx;
	int ret;

	ctx = devm_kzalloc(dev, sizeof(*ctx), GFP_KERNEL);
	if (!ctx)
		return -ENOMEM;

	ctx->reset = devm_gpiod_get_optional(dev, "reset", GPIOD_OUT_LOW);
	if (IS_ERR(ctx->reset)) {
		dev_err(dev, "failed to get reset GPIO: %ld\n",
			PTR_ERR(ctx->reset));
		return PTR_ERR(ctx->reset);
	}

	ctx->dsi = dsi;
	mipi_dsi_set_drvdata(dsi, ctx);

	dsi->lanes     = 2;
	dsi->format    = MIPI_DSI_FMT_RGB888;
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
		dev_err(dev, "failed to attach to DSI host: %d\n", ret);
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
	{ /* sentinel */ }
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
MODULE_DESCRIPTION("Waveshare 3.5-inch DSI (E) panel — ST7701S");
MODULE_LICENSE("GPL");
