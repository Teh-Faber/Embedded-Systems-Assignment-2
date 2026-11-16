#!/usr/bin/env python3
"""Compare CNN and SNN results for the same test images."""

import argparse
import csv
import json
from pathlib import Path
from statistics import median
import sys


MODELS = ("cnn", "snn")
TEST_RESULT = 128
BASE_COLUMNS = ("image_id", "image_path", "true_id", "true_label", "comparison")
MODEL_COLUMNS = ("status", "prediction_id", "prediction", "correct", "infer_us",
                 "prep_us", "dfx_us", "scores")
COLUMNS = (*BASE_COLUMNS, *(f"{model}_{key}" for model in MODELS for key in MODEL_COLUMNS),
           "transport_errors")
OUTCOMES = {
    (True, True): "both_correct",
    (True, False): "cnn_only_correct",
    (False, True): "snn_only_correct",
    (False, False): "both_wrong",
}


def load_logs(paths):
    """Join single-model or paired runs by image ID, checking the test manifest."""
    classes = None
    images = {}
    ids_by_path = {}
    for path in paths:
        with path.open(encoding="utf-8") as log:
            for line_number, line in enumerate(log, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    names = tuple(record["class_names"])
                    if classes is None:
                        classes = names
                    if names != classes or len(set(names)) != 4 or len(names) != 4:
                        raise ValueError("Class ID order must match across runs.")
                    image_id = record["image_id"]
                    image_path = record["image_path"]
                    true_id = record["true_class_id"]
                    if not isinstance(true_id, int) or not 0 <= true_id < len(classes):
                        raise ValueError("Invalid true class ID.")
                    if ids_by_path.setdefault(image_path, image_id) != image_id:
                        raise ValueError(f"Image ID changed for {image_path}.")
                    image = images.setdefault(image_id, {
                        "image_path": image_path, "true_id": true_id,
                        "results": {}, "dfx_us": {}, "errors": [],
                    })
                    if (image_path, true_id) != (image["image_path"], image["true_id"]):
                        raise ValueError(f"Image ID {image_id} refers to different images across runs.")
                    for model, result in record["results"].items():
                        if model not in MODELS or model in image["results"]:
                            raise ValueError(f"Unexpected or duplicate {model} reply for {image_path}.")
                        if result["status"] == 0 and not 0 <= result["class_id"] < len(classes):
                            raise ValueError(f"Invalid predicted class ID for {model}.")
                        image["results"][model] = result
                    image["dfx_us"].update(record.get("dfx_us", {}))
                    if record.get("transport_error"):
                        image["errors"].append(record["transport_error"])
                except (AttributeError, KeyError, TypeError, ValueError) as error:
                    raise ValueError(f"{path}:{line_number}: {error}") from error

    if not images:
        raise ValueError("No image records were found.")
    return classes, images


def model_columns(result, dfx_us, true_id, classes):
    """Map one reply to CSV fields; only status zero can be scored."""
    columns = {"dfx_us": dfx_us}
    if result is None:
        columns["status"] = "missing"
    elif result["status"] == TEST_RESULT:
        columns["status"] = "protocol_test"
    elif result["status"] != 0:
        columns["status"] = f"error:{result['status']}"
    else:
        prediction = result["class_id"]
        columns.update(status="ok", prediction_id=prediction, prediction=classes[prediction],
                       correct=prediction == true_id, infer_us=result["infer_us"],
                       prep_us=result["prep_us"], scores=json.dumps(result["scores"]))
    return columns


def compare_images(classes, images):
    """Produce one row per image, including errors and incomplete pairs."""
    rows = []
    for image_id, image in sorted(images.items()):
        true_id = image["true_id"]
        row = dict(image_id=image_id, image_path=image["image_path"], true_id=true_id,
                   true_label=classes[true_id], transport_errors=" | ".join(image["errors"]))
        for model in MODELS:
            columns = model_columns(image["results"].get(model), image["dfx_us"].get(model),
                                    true_id, classes)
            row.update((f"{model}_{key}", value) for key, value in columns.items())
        pair = row.get("cnn_correct"), row.get("snn_correct")
        row["comparison"] = OUTCOMES.get(pair, "incomplete_pair")
        rows.append(row)
    return rows


def print_summary(classes, rows):
    """Score both models on exactly the same images and print two matrices."""
    paired = [row for row in rows if row["comparison"] != "incomplete_pair"]
    print(f"Images: {len(rows)}  Valid pairs: {len(paired)}  Incomplete pairs: {len(rows) - len(paired)}")
    if not paired:
        print("No valid CNN/SNN pairs to score. See the CSV status columns.")
        return
    for outcome in OUTCOMES.values():
        print(f"{outcome}: {sum(row['comparison'] == outcome for row in paired)}")

    width = max(map(len, classes))
    for model in MODELS:
        matrix = [[0] * len(classes) for _ in classes]
        for row in paired:
            matrix[row["true_id"]][row[f"{model}_prediction_id"]] += 1
        accuracy = sum(row[f"{model}_correct"] for row in paired) / len(paired)
        inference = median(row[f"{model}_infer_us"] for row in paired)
        preprocessing = median(row[f"{model}_prep_us"] for row in paired)
        print(f"\n{model.upper()} accuracy: {accuracy:.2%}  "
              f"Median inference: {inference:g} us  Median preparation: {preprocessing:g} us")
        print("Confusion matrix (rows=true, columns=predicted)")
        print(" " * (width + 2) + "  ".join(f"{name:>{width}}" for name in classes))
        for label, counts in zip(classes, matrix):
            print(f"{label:<{width}}  " + "  ".join(f"{count:>{width}}" for count in counts))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", nargs="+", type=Path, help="A paired log or separate model logs")
    parser.add_argument("--output", type=Path, default=Path("comparison.csv"), help="New per-image CSV")
    parser.add_argument("--image", help="Also display one image by numeric ID or relative path")
    args = parser.parse_args()
    try:
        classes, images = load_logs(args.logs)
        rows = compare_images(classes, images)
        if args.image is not None:
            image = next((row for row in rows if args.image in
                          (str(row["image_id"]), row["image_path"])), None)
            if image is None:
                raise ValueError(f"Image {args.image!r} was not found.")
            print(json.dumps(image, ensure_ascii=False, indent=2))
        with args.output.open("x", encoding="utf-8-sig", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        print_summary(classes, rows)
        print(f"Per-image comparison saved to {args.output}.")
        return 0
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
