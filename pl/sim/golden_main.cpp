// Golden model for the RTL testbench: runs the HLS C source of ds_cnn on
// Q8.8 images and prints pred + raw logit bits, bit-exact with the RTL.
//
//   golden <images.mem> <n_images>
// images.mem: one 16-bit hex word per line, 4800 words per image.
// Output: one line per image: "<pred> <l0> <l1> <l2> <l3>" (hex).
#include <cstdio>
#include <cstdlib>
#include "ap_fixed.h"

typedef ap_fixed<16,8> data_t;
static const int N_IN = 60 * 80;
static const int N_CLASSES = 4;

extern void ds_cnn(const data_t *input_img, int *pred_out, data_t logits_out[N_CLASSES]);

int main(int argc, char **argv)
{
    if (argc < 3) { std::fprintf(stderr, "usage: golden images.mem n\n"); return 2; }
    FILE *f = std::fopen(argv[1], "r");
    if (!f) { std::perror(argv[1]); return 2; }
    int n = std::atoi(argv[2]);
    static data_t img[N_IN];
    for (int k = 0; k < n; k++) {
        for (int i = 0; i < N_IN; i++) {
            unsigned v;
            if (std::fscanf(f, "%x", &v) != 1) { std::fprintf(stderr, "short file\n"); return 1; }
            img[i].range(15, 0) = v & 0xFFFF;
        }
        int pred = -1;
        data_t lg[N_CLASSES];
        ds_cnn(img, &pred, lg);
        std::printf("%02x", pred & 0xFF);
        for (int c = 0; c < N_CLASSES; c++)
            std::printf(" %04x", (unsigned)lg[c].range(15, 0).to_uint() & 0xFFFF);
        std::printf("\n");
    }
    return 0;
}
