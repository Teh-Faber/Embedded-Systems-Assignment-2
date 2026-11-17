
#include <iostream>
#include <fstream>
#include <string>
#include <vector>
#include <algorithm>
#include <dirent.h>
#include <sys/stat.h>

#include "ap_int.h"
#include "fpga_weights.h"

typedef ap_int<16> data_t;
typedef ap_int<32> acc_t;

// Must match the top function in snn_mlp.cpp.
void snn_mlp(
    const ap_int<16> *input_img,
    ap_uint<8> *pred_out,
    ap_int<32> logits_out[NUM_CLASSES]
);

static const int N_PIXELS = IMG_H * IMG_W;   // 4800

// Same names and order as CLASS_FOLDERS in snn_mlp.py.
static int label_from_folder(const std::string &name)
{
    if (name == "Circle") return 0;
    if (name == "Figure Eight") return 1;
    if (name == "Square") return 2;
    if (name == "Triangle") return 3;
    return -1;
}

static const char* class_name(int label)
{
    if (label == 0) return "Circle";
    if (label == 1) return "Figure Eight";
    if (label == 2) return "Square";
    if (label == 3) return "Triangle";
    return "Unknown";
}

static bool is_directory(const std::string &path)
{
    struct stat st;
    if (stat(path.c_str(), &st) != 0) return false;
    return S_ISDIR(st.st_mode);
}

static bool has_txt_extension(const std::string &name)
{
    if (name.size() < 4) return false;
    return name.substr(name.size() - 4) == ".txt";
}

// Reads one image as text: 4800 grayscale values 0..255, row-major, separated by
// spaces and/or newlines (same format as the CNN testbench: 60 lines of 80 values).
// Converts to the Q8.8 format the PS writes into DDR: q = round((u8/255 - 0.5)/0.25 * 256).
static bool load_txt_image_q8_8(
    const std::string &txt_path,
    data_t image_mem[N_PIXELS]
) {
    std::ifstream file(txt_path.c_str());

    if (!file.is_open()) {
        std::cerr << "Could not open file: " << txt_path << std::endl;
        return false;
    }

    int count = 0;
    int value = 0;

    while (file >> value) {
        if (count >= N_PIXELS) {
            std::cerr << "Too many pixel values in " << txt_path << std::endl;
            return false;
        }
        if (value < 0 || value > 255) {
            std::cerr << "Pixel out of range 0..255 in " << txt_path << std::endl;
            return false;
        }

        image_mem[count] = (data_t)((1024 * value + 127) / 255 - 512);
        count++;
    }

    if (count != N_PIXELS) {
        std::cerr << "Bad pixel count in " << txt_path << std::endl;
        std::cerr << "Expected " << N_PIXELS
                  << " values, got " << count << std::endl;
        return false;
    }

    return true;
}

static std::vector<std::string> list_subfolders(const std::string &root)
{
    std::vector<std::string> folders;

    DIR *dir = opendir(root.c_str());
    if (!dir) {
        std::cerr << "Could not open root folder: " << root << std::endl;
        return folders;
    }

    struct dirent *entry;

    while ((entry = readdir(dir)) != NULL) {
        std::string name = entry->d_name;

        if (name == "." || name == "..") continue;

        std::string full_path = root + "/" + name;

        if (is_directory(full_path)) {
            folders.push_back(name);
        }
    }

    closedir(dir);

    std::sort(folders.begin(), folders.end());
    return folders;
}

static std::vector<std::string> list_txt_files(const std::string &folder_path)
{
    std::vector<std::string> files;

    DIR *dir = opendir(folder_path.c_str());
    if (!dir) {
        std::cerr << "Could not open folder: " << folder_path << std::endl;
        return files;
    }

    struct dirent *entry;

    while ((entry = readdir(dir)) != NULL) {
        std::string name = entry->d_name;

        if (name == "." || name == "..") continue;
        if (!has_txt_extension(name)) continue;

        std::string full_path = folder_path + "/" + name;
        files.push_back(full_path);
    }

    closedir(dir);

    std::sort(files.begin(), files.end());
    return files;
}

int main()
{
    const int N = 10;

    // <repo>/data/shapes_test_txt: one subfolder per class, each holding .txt images
    // (see loader above). Located relative to this file, like the repo version.
    std::string tb_file = __FILE__;
    size_t tb_sep = tb_file.find_last_of("/\\");
    const std::string root =
        (tb_sep == std::string::npos ? std::string(".") : tb_file.substr(0, tb_sep))
        + "/../data/shapes_test_txt";

    int total = 0;
    int correct = 0;

    int confusion[NUM_CLASSES][NUM_CLASSES] = {0};

    std::vector<std::string> folders = list_subfolders(root);

    for (size_t fi = 0; fi < folders.size(); fi++) {
        std::string folder_name = folders[fi];
        std::string folder_path = root + "/" + folder_name;

        int true_label = label_from_folder(folder_name);

        if (true_label < 0) {
            std::cout << "Skipping unknown folder: "
                      << folder_name << std::endl;
            continue;
        }

        std::vector<std::string> txt_files =
            list_txt_files(folder_path);

        int files_to_use =
            std::min((int)txt_files.size(), N);

        std::cout << "\nClass: " << folder_name
                  << " | using " << files_to_use
                  << " files" << std::endl;

        for (int i = 0; i < files_to_use; i++) {
            std::string txt_path = txt_files[i];

            data_t image_mem[N_PIXELS];

            ap_uint<8> prediction = 0;
            acc_t logits_out[NUM_CLASSES];

            bool ok = load_txt_image_q8_8(
                txt_path,
                image_mem
            );

            if (!ok) continue;


            snn_mlp(
                image_mem,
                &prediction,
                logits_out
            );

            int pred = (int)prediction;

            total++;

            if (pred == true_label) {
                correct++;
            }

            if (pred >= 0 && pred < NUM_CLASSES) {
                confusion[true_label][pred]++;
            }

            std::cout << "[" << total << "] "
                      << txt_path
                      << " | true=" << class_name(true_label)
                      << " | pred=" << class_name(pred)
                      << " | logits=[";

            for (int k = 0; k < NUM_CLASSES; k++) {
                std::cout << (int)logits_out[k];

                if (k != NUM_CLASSES - 1)
                    std::cout << ", ";
            }

            std::cout << "]"
                      << std::endl;

        }
    }

    std::cout << "\n====================================" << std::endl;
    std::cout << "Total tested: " << total << std::endl;
    std::cout << "Correct: " << correct << std::endl;

    if (total > 0) {
        float acc = 100.0f * correct / total;
        std::cout << "Accuracy: " << acc << "%" << std::endl;
    }

    std::cout << "\nConfusion Matrix" << std::endl;
    std::cout << "(rows=true, cols=pred)" << std::endl;

    for (int i = 0; i < NUM_CLASSES; i++) {
        for (int j = 0; j < NUM_CLASSES; j++) {
            std::cout << confusion[i][j] << " ";
        }
        std::cout << std::endl;
    }

    return 0;
}
