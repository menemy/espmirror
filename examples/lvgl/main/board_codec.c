/* Copyright (c) 2026 Maksim Nagaev
 * SPDX-License-Identifier: Apache-2.0
 */
#include "sdkconfig.h"

#if CONFIG_EXAMPLE_BOARD_WAVESHARE_216 || CONFIG_EXAMPLE_BOARD_WAVESHARE_175C || CONFIG_EXAMPLE_BOARD_WAVESHARE_349 || CONFIG_EXAMPLE_BOARD_WAVESHARE_154G || CONFIG_EXAMPLE_BOARD_WAVESHARE_P4_35

#include "board_codec.h"
#include "driver/i2s_std.h"
#include "esp_check.h"
#include "esp_codec_dev.h"
#include "esp_codec_dev_defaults.h"

static esp_codec_dev_handle_t s_speaker, s_microphone;
static bool s_dual_mic;

esp_err_t board_codec_start(const board_codec_config_t *config) {
#if CONFIG_ESPMIRROR_AUDIO
    static const char *TAG = "board_codec";
    i2s_chan_handle_t tx, rx;
    i2s_chan_config_t channels = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    channels.auto_clear = true;
    ESP_RETURN_ON_ERROR(i2s_new_channel(&channels, &tx, &rx), TAG, "channels");
    const i2s_std_config_t bus = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(16000),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {.mclk = config->mclk, .bclk = config->bclk, .ws = config->ws, .dout = config->dout, .din = config->din},
    };
    ESP_RETURN_ON_ERROR(i2s_channel_init_std_mode(tx, &bus), TAG, "TX init");
    ESP_RETURN_ON_ERROR(i2s_channel_init_std_mode(rx, &bus), TAG, "RX init");
    ESP_RETURN_ON_ERROR(i2s_channel_enable(tx), TAG, "TX enable");
    ESP_RETURN_ON_ERROR(i2s_channel_enable(rx), TAG, "RX enable");
    audio_codec_i2s_cfg_t data_cfg = {.port = I2S_NUM_0, .tx_handle = tx, .rx_handle = rx};
    const audio_codec_data_if_t *data = audio_codec_new_i2s_data(&data_cfg);
    audio_codec_i2c_cfg_t control_cfg = {.port = I2C_NUM_0, .bus_handle = config->bus, .addr = ES8311_CODEC_DEFAULT_ADDR};
    const audio_codec_ctrl_if_t *dac_control = audio_codec_new_i2c_ctrl(&control_cfg);
    const audio_codec_gpio_if_t *gpio = audio_codec_new_gpio();
    ESP_RETURN_ON_FALSE(data && dac_control && gpio, ESP_ERR_NO_MEM, TAG, "interfaces");
    es8311_codec_cfg_t dac_cfg = {
        .ctrl_if = dac_control, .gpio_if = gpio, .pa_pin = config->amplifier, .use_mclk = true,
        .codec_mode = config->es7210 ? ESP_CODEC_DEV_WORK_MODE_DAC : ESP_CODEC_DEV_WORK_MODE_BOTH,
        .hw_gain = {.pa_voltage = 5.0, .codec_dac_voltage = 3.3},
    };
    const audio_codec_if_t *dac = es8311_codec_new(&dac_cfg);
    ESP_RETURN_ON_FALSE(dac, ESP_FAIL, TAG, "ES8311");
    const audio_codec_if_t *adc = dac;
    if (config->es7210) {
        control_cfg.addr = ES7210_CODEC_DEFAULT_ADDR;
        const audio_codec_ctrl_if_t *adc_control = audio_codec_new_i2c_ctrl(&control_cfg);
        ESP_RETURN_ON_FALSE(adc_control, ESP_ERR_NO_MEM, TAG, "ADC interface");
        es7210_codec_cfg_t adc_cfg = {.ctrl_if = adc_control, .mic_selected = ES7210_SEL_MIC1 | ES7210_SEL_MIC2};
        adc = es7210_codec_new(&adc_cfg);
        ESP_RETURN_ON_FALSE(adc, ESP_FAIL, TAG, "ES7210");
    }
    esp_codec_dev_cfg_t out = {.dev_type = ESP_CODEC_DEV_TYPE_OUT, .codec_if = dac, .data_if = data};
    esp_codec_dev_cfg_t in = {.dev_type = ESP_CODEC_DEV_TYPE_IN, .codec_if = adc, .data_if = data};
    s_speaker = esp_codec_dev_new(&out);
    s_microphone = esp_codec_dev_new(&in);
    ESP_RETURN_ON_FALSE(s_speaker && s_microphone, ESP_ERR_NO_MEM, TAG, "devices");
    esp_codec_dev_sample_info_t format = {.sample_rate = 16000, .channel = 2, .bits_per_sample = 16};
    ESP_RETURN_ON_ERROR(esp_codec_dev_open(s_speaker, &format), TAG, "speaker open");
    ESP_RETURN_ON_ERROR(esp_codec_dev_open(s_microphone, &format), TAG, "mic open");
    ESP_RETURN_ON_ERROR(esp_codec_dev_set_out_vol(s_speaker, 60), TAG, "volume");
    ESP_RETURN_ON_ERROR(esp_codec_dev_set_in_gain(s_microphone, 24), TAG, "gain");
    s_dual_mic = config->es7210;
    return ESP_OK;
#else
    (void)config;
    return ESP_ERR_NOT_SUPPORTED;
#endif
}

size_t board_codec_read(int16_t *mono, size_t frames) {
    if (!s_microphone || frames > 320) return 0;
    int16_t stereo[640];
    if (esp_codec_dev_read(s_microphone, stereo, frames * 4) != ESP_CODEC_DEV_OK) return 0;
    for (size_t i = 0; i < frames; i++) {
        mono[i] = s_dual_mic ? ((int32_t)stereo[i * 2] + stereo[i * 2 + 1]) / 2 : stereo[i * 2];
    }
    return frames;
}

void board_codec_write(const int16_t *mono, size_t frames) {
    if (!s_speaker) return;
    int16_t stereo[640];
    while (frames) {
        size_t count = frames < 320 ? frames : 320;
        for (size_t i = 0; i < count; i++) stereo[i * 2] = stereo[i * 2 + 1] = mono[i];
        if (esp_codec_dev_write(s_speaker, stereo, count * 4) != ESP_CODEC_DEV_OK) return;
        mono += count;
        frames -= count;
    }
}

#endif
