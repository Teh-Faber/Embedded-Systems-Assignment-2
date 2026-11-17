

// ============================================================================
// SNN Inference for Vitis HLS
// Resource-reduced + FC1 feature-parallel version
//
// Input : the same 60x80 Q8.8 image buffer the CNN reads from DDR.
// Stage 1: Sobel ON/OFF feature extraction in hardware (bit-exact with snn_mlp.py).
// Stage 2: spiking MLP (unchanged).
// Supports T_STEPS = 1 only.
// ============================================================================

#include "ap_int.h"
#include "fpga_weights.h"

typedef ap_int<16> data_t;
typedef ap_int<16> weight_t;
typedef ap_int<32> acc_t;
typedef ap_uint<1> spike_t;

// ---------------------------------------------------------------------------
// LIF parameters
// ---------------------------------------------------------------------------
static const acc_t BETA_NUM = 115;
static const acc_t LIF_THRESHOLD = WEIGHT_SCALE;

// ---------------------------------------------------------------------------
// Unroll factors
// ---------------------------------------------------------------------------
static const int FC1_NEURON_UNROLL = 4;
static const int FC2_NEURON_UNROLL = 4;
static const int FC3_OUT_UNROLL    = 4;

// ============================================================================
// LIF update
// ============================================================================
static spike_t lif_update(acc_t input_current, acc_t &mem)
{
#pragma HLS INLINE

    // Determine reset from the previous membrane voltage.
    spike_t previous_spike = (mem > LIF_THRESHOLD);

    // Integrate current and apply the delayed reset.
    mem = ((mem * BETA_NUM) >> 7) + input_current;

    if (previous_spike) {
        mem -= LIF_THRESHOLD;
    }

    // Generate the current spike without immediately resetting mem.
    return (mem > LIF_THRESHOLD) ? spike_t(1) : spike_t(0);
}

// ============================================================================
// Feature extraction: image -> 384 Sobel ON/OFF cell densities
// Mirrors image_to_spikes() + spikes_to_snn_mlp_features() in snn_mlp.py (T = 1).
// ============================================================================
static const int N_PIX   = IMG_H * IMG_W;        // 4800
static const int CELL_H  = IMG_H / GRID_H;       // 5
static const int CELL_W  = IMG_W / GRID_W;       // 5
static const int N_CELLS = GRID_H * GRID_W;      // 192

// Pixels are Q8.8 in [-512, 512] -> |sobel_x + sobel_y| <= 8192.
static const int HIST_SIZE = 8193;

typedef ap_int<16>  pix_t;    // Q8.8 pixel
typedef ap_int<15>  edge_t;   // sobel_x + sobel_y, -8192..8192
typedef ap_uint<14> mag_t;    // |edge|, 0..8192
typedef ap_uint<13> cnt_t;    // pixel counts, 0..4800

static void extract_features(
    const pix_t *image_mem,
    data_t features[T_STEPS][N_FEATURES]
) {
#pragma HLS INLINE off

    pix_t  img[IMG_H][IMG_W];
    edge_t edge[N_PIX];
    // Histograms of ON / OFF edge strength. Must start at zero:
    // zero at configuration, and step 3 clears them again after every image.
    static cnt_t hist_on[HIST_SIZE];
    static cnt_t hist_off[HIST_SIZE];
    ap_uint<5> cell_on[N_CELLS];    // spike count per cell, 0..25
    ap_uint<5> cell_off[N_CELLS];

// Rows y-1, y, y+1 land in three different memories -> one read each per cycle.
#pragma HLS ARRAY_PARTITION variable=img cyclic factor=3 dim=1
#pragma HLS BIND_STORAGE variable=img      type=ram_2p impl=bram
#pragma HLS BIND_STORAGE variable=edge     type=ram_2p impl=bram
#pragma HLS BIND_STORAGE variable=hist_on  type=ram_2p impl=bram
#pragma HLS BIND_STORAGE variable=hist_off type=ram_2p impl=bram

    // ---- 1. Read the image from DDR (one burst) ----
    READ_IMG:
    for (int i = 0; i < N_PIX; i++) {
#pragma HLS PIPELINE II=1
        img[i / IMG_W][i % IMG_W] = image_mem[i];
    }

    // ---- 2. 3x3 Sobel (x + y) and edge-strength histograms ----
    // Border handling = reflect-101, like cv2.Sobel: index -1 -> 1, index N -> N-2.
    // A 3x3 window slides along each row; one extra step per row flushes the last pixel.
    SOBEL_ROW:
    for (int y = 0; y < IMG_H; y++) {
        const int r_top = (y == 0)         ? 1         : y - 1;
        const int r_bot = (y == IMG_H - 1) ? IMG_H - 2 : y + 1;

        pix_t l_top = 0, l_mid = 0, l_bot = 0;   // left column of the window
        pix_t m_top = 0, m_mid = 0, m_bot = 0;   // middle column
        pix_t r_tp  = 0, r_md  = 0, r_bt  = 0;   // right column

        SOBEL_COL:
        for (int xi = 0; xi <= IMG_W; xi++) {
#pragma HLS PIPELINE II=2
            // New right column (reflected past the right border).
            const int c = (xi < IMG_W) ? xi : IMG_W - 2;
            l_top = m_top; l_mid = m_mid; l_bot = m_bot;
            m_top = r_tp;  m_mid = r_md;  m_bot = r_bt;
            r_tp = img[r_top][c];
            r_md = img[y][c];
            r_bt = img[r_bot][c];

            if (xi >= 1) {
                const int x = xi - 1;             // pixel whose window is now complete
                // Left border: column -1 is the reflection of column 1 (= current right column).
                const pix_t a_top = (x == 0) ? r_tp : l_top;
                const pix_t a_mid = (x == 0) ? r_md : l_mid;
                const pix_t a_bot = (x == 0) ? r_bt : l_bot;

                const ap_int<19> gx = (r_tp - a_top) + 2 * (r_md - a_mid) + (r_bt - a_bot);
                const ap_int<19> gy = (a_bot + 2 * m_bot + r_bt) - (a_top + 2 * m_top + r_tp);
                const edge_t s = gx + gy;

                edge[y * IMG_W + x] = s;

                const mag_t pos = (s > 0) ? mag_t(s)  : mag_t(0);
                const mag_t neg = (s < 0) ? mag_t(-s) : mag_t(0);
                hist_on[pos]  = hist_on[pos]  + 1;
                hist_off[neg] = hist_off[neg] + 1;
            }
        }
    }

    // ---- 3. Percentile thresholds from the histograms (and clear them) ----
    // Threshold = SOBEL_RANK-th smallest value = first bin where the running count exceeds SOBEL_RANK.
    mag_t thr_on = 0, thr_off = 0;
    cnt_t cum_on = 0, cum_off = 0;
    bool  found_on = false, found_off = false;

    FIND_THR:
    for (int v = 0; v < HIST_SIZE; v++) {
#pragma HLS PIPELINE II=1
        cum_on  += hist_on[v];
        cum_off += hist_off[v];
        hist_on[v]  = 0;
        hist_off[v] = 0;

        if (!found_on && cum_on > SOBEL_RANK)   { thr_on  = v; found_on  = true; }
        if (!found_off && cum_off > SOBEL_RANK) { thr_off = v; found_off = true; }
    }

    // ---- 4. Count ON / OFF spikes in each 5x5 cell ----
    CELL_Y:
    for (int gy = 0; gy < GRID_H; gy++) {
        CELL_X:
        for (int gx = 0; gx < GRID_W; gx++) {
            ap_uint<5> n_on = 0, n_off = 0;

            PIX_Y:
            for (int dy = 0; dy < CELL_H; dy++) {
                PIX_X:
                for (int dx = 0; dx < CELL_W; dx++) {
#pragma HLS PIPELINE II=1
                    const edge_t s = edge[(gy * CELL_H + dy) * IMG_W + gx * CELL_W + dx];
                    if (s > 0 && mag_t(s) > thr_on)   n_on++;
                    if (s < 0 && mag_t(-s) > thr_off) n_off++;
                }
            }
            cell_on[gy * GRID_W + gx]  = n_on;
            cell_off[gy * GRID_W + gx] = n_off;
        }
    }

    // ---- 5. Counts -> scaled features, order [cell][ON, OFF] as in Python ----
    WRITE_FEAT:
    for (int c = 0; c < N_CELLS; c++) {
#pragma HLS PIPELINE II=2
        features[0][2 * c]     = feat_lut[cell_on[c]];
        features[0][2 * c + 1] = feat_lut[cell_off[c]];
    }
}

// ============================================================================
// FC1 + LIF
// ============================================================================
static void fc1_lif(
    data_t x[N_FEATURES],
    spike_t spk1[HIDDEN1],
    acc_t mem1[HIDDEN1]
) {
#pragma HLS INLINE off

#pragma HLS ARRAY_PARTITION variable=fc1_w cyclic factor=FC1_NEURON_UNROLL dim=1
#pragma HLS ARRAY_PARTITION variable=fc1_w cyclic factor=2 dim=2
#pragma HLS ARRAY_PARTITION variable=fc1_b cyclic factor=FC1_NEURON_UNROLL dim=1

#pragma HLS ARRAY_PARTITION variable=x cyclic factor=2 dim=1
#pragma HLS ARRAY_PARTITION variable=mem1 cyclic factor=FC1_NEURON_UNROLL dim=1
#pragma HLS ARRAY_PARTITION variable=spk1 cyclic factor=FC1_NEURON_UNROLL dim=1

    for (int h = 0; h < HIDDEN1; h++) {
#pragma HLS UNROLL factor=FC1_NEURON_UNROLL

        // Accumulate full-precision products (scale 128*128) and rescale once.
        // Shifting each product individually rounds toward -inf ~100 times per
        // neuron, biasing the current negative and costing ~19% accuracy.
        acc_t acc = 0;

        for (int i = 0; i < N_FEATURES; i += 2) {
#pragma HLS PIPELINE II=1

            if (x[i] != 0) {
                acc += (acc_t)x[i] * (acc_t)fc1_w[h][i];
            }

            if (x[i + 1] != 0) {
                acc += (acc_t)x[i + 1] * (acc_t)fc1_w[h][i + 1];
            }
        }

        acc = ((acc + 64) >> 7) + fc1_b[h];

        spk1[h] = lif_update(acc, mem1[h]);
    }
}

// ============================================================================
// FC2 + LIF
// ============================================================================
static void fc2_lif(
    spike_t spk1[HIDDEN1],
    spike_t spk2[HIDDEN2],
    acc_t mem2[HIDDEN2]
) {
#pragma HLS INLINE off

#pragma HLS ARRAY_PARTITION variable=fc2_w cyclic factor=FC2_NEURON_UNROLL dim=1
#pragma HLS ARRAY_PARTITION variable=fc2_b cyclic factor=FC2_NEURON_UNROLL dim=1

#pragma HLS ARRAY_PARTITION variable=mem2 cyclic factor=FC2_NEURON_UNROLL dim=1
#pragma HLS ARRAY_PARTITION variable=spk2 cyclic factor=FC2_NEURON_UNROLL dim=1
#pragma HLS ARRAY_PARTITION variable=spk1 cyclic factor=FC1_NEURON_UNROLL dim=1

    for (int h = 0; h < HIDDEN2; h++) {
#pragma HLS UNROLL factor=FC2_NEURON_UNROLL

        acc_t acc = fc2_b[h];

        for (int i = 0; i < HIDDEN1; i++) {
#pragma HLS PIPELINE II=1

            if (spk1[i]) {
                acc += fc2_w[h][i];
            }
        }

        spk2[h] = lif_update(acc, mem2[h]);
    }
}

// ============================================================================
// FC3 output layer
// ============================================================================
static void fc3_lif(
    spike_t spk2[HIDDEN2],
    spike_t spk3[NUM_CLASSES],
    acc_t mem3[NUM_CLASSES]
) {
#pragma HLS INLINE off

#pragma HLS ARRAY_PARTITION variable=fc3_w complete dim=1
#pragma HLS ARRAY_PARTITION variable=fc3_b complete dim=1
#pragma HLS ARRAY_PARTITION variable=spk3 complete dim=1
#pragma HLS ARRAY_PARTITION variable=mem3 complete dim=1

    for (int o = 0; o < NUM_CLASSES; o++) {
#pragma HLS UNROLL factor=FC3_OUT_UNROLL

        acc_t acc = fc3_b[o];

        for (int i = 0; i < HIDDEN2; i++) {
#pragma HLS PIPELINE II=1
            if (spk2[i]) {
                acc += fc3_w[o][i];
            }
        }

        spk3[o] = lif_update(acc, mem3[o]);
    }
}

// ============================================================================
// Argmax
// ============================================================================
static int argmax_logits(acc_t logits_sum[NUM_CLASSES])
{
#pragma HLS INLINE

    int best = 0;
    acc_t best_val = logits_sum[0];

    for (int i = 1; i < NUM_CLASSES; i++) {
#pragma HLS UNROLL

        if (logits_sum[i] > best_val) {
            best_val = logits_sum[i];
            best = i;
        }
    }

    return best;
}

// ============================================================================
// SNN core
// ============================================================================
static int snn_core(
    data_t features[T_STEPS][N_FEATURES],
    acc_t logits_out[NUM_CLASSES]
)
{
#pragma HLS INLINE off

    acc_t mem1[HIDDEN1];
    acc_t mem2[HIDDEN2];
    acc_t mem3[NUM_CLASSES];

    spike_t spk1[HIDDEN1];
    spike_t spk2[HIDDEN2];
    spike_t spk3[NUM_CLASSES];

    acc_t logits_sum[NUM_CLASSES];

#pragma HLS ARRAY_PARTITION variable=mem1 cyclic factor=FC1_NEURON_UNROLL dim=1
#pragma HLS ARRAY_PARTITION variable=mem2 cyclic factor=FC2_NEURON_UNROLL dim=1
#pragma HLS ARRAY_PARTITION variable=mem3 complete dim=1

#pragma HLS ARRAY_PARTITION variable=spk1 cyclic factor=FC1_NEURON_UNROLL dim=1
#pragma HLS ARRAY_PARTITION variable=spk2 cyclic factor=FC2_NEURON_UNROLL dim=1
#pragma HLS ARRAY_PARTITION variable=spk3 complete dim=1

#pragma HLS ARRAY_PARTITION variable=logits_sum complete dim=1

    for (int i = 0; i < HIDDEN1; i++) {
#pragma HLS PIPELINE II=1
        mem1[i] = 0;
        spk1[i] = 0;
    }

    for (int i = 0; i < HIDDEN2; i++) {
#pragma HLS PIPELINE II=1
        mem2[i] = 0;
        spk2[i] = 0;
    }

    for (int i = 0; i < NUM_CLASSES; i++) {
#pragma HLS UNROLL
        mem3[i] = 0;
        spk3[i] = 0;
        logits_sum[i] = 0;
    }

    for (int t = 0; t < T_STEPS; t++) {
        fc1_lif(features[t], spk1, mem1);
        fc2_lif(spk1, spk2, mem2);
        fc3_lif(spk2, spk3, mem3);

        for (int o = 0; o < NUM_CLASSES; o++) {
#pragma HLS UNROLL
            logits_sum[o] += mem3[o];
        }
    }

    for (int o = 0; o < NUM_CLASSES; o++) {
    #pragma HLS UNROLL
        logits_out[o] = logits_sum[o];
    }

    return argmax_logits(logits_sum);
}


// ============================================================================
// Top-level HLS function
// PS writes the 60x80 Q8.8 image into DDR (same buffer as for the CNN).
// HLS reads it over AXI, extracts the features, runs the SNN,
// and returns prediction + logits through AXI-Lite.
//
// DFX interface (see pl/ip/README.md): same argument names and order as
// ds_cnn(input_img, pred_out, logits_out), one s_axilite bundle, one
// read-only m_axi port with 16-bit elements.
// ============================================================================
void snn_mlp(
    const ap_int<16> *input_img,             // 4800 px, Q8.8, row-major 60x80
    ap_uint<8> *pred_out,                    // predicted class index
    ap_int<32> logits_out[NUM_CLASSES]       // raw integer logits
) {
#pragma HLS INTERFACE m_axi     port=input_img  offset=slave bundle=gmem depth=4800
#pragma HLS INTERFACE s_axilite port=input_img  bundle=control
#pragma HLS INTERFACE s_axilite port=pred_out   bundle=control
#pragma HLS INTERFACE s_axilite port=logits_out bundle=control
#pragma HLS INTERFACE s_axilite port=return     bundle=control

    data_t features[T_STEPS][N_FEATURES];

#pragma HLS BIND_STORAGE variable=features type=ram_2p impl=bram
#pragma HLS ARRAY_PARTITION variable=features complete dim=1

    extract_features(input_img, features);

    acc_t local_logits[NUM_CLASSES];
    #pragma HLS ARRAY_PARTITION variable=local_logits complete dim=1

    int best = snn_core(features, local_logits);

    *pred_out = (ap_uint<8>)best;

    for (int i = 0; i < NUM_CLASSES; i++) {
    #pragma HLS UNROLL
        logits_out[i] = local_logits[i];
    }
}
