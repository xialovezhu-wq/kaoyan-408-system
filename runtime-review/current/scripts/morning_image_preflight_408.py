#!/usr/bin/env python3
"""Inspect a frozen morning plan's images once during preparation, without edits."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path

from PIL import Image


def inspect_plan(plan, details):
    details = Path(details).resolve()
    items = plan.get("learning_items")
    if items is None:
        items = plan.get("selected_items", []) + plan.get("supplemental_items", [])
    inspected = []
    cache = {}
    for item in items:
        selection = item.get("selection", {})
        assets = []
        from d_asset_delivery_408 import prepared_question_assets
        refs = prepared_question_assets(plan, item) or selection.get("question_asset_refs", [])
        for ref in refs:
            path = Path(ref["path"]).resolve() if ref.get("path") else (details / ref["ref"]).resolve()
            key = (str(path), ref.get("sha256"))
            if key not in cache:
                row = dict(path=str(path), ref=ref["ref"], expected_sha256=ref.get("sha256"),
                           warnings=[], decodable=False)
                try:
                    path.relative_to(details)
                    raw = path.read_bytes()
                    row.update(sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw))
                    if row["sha256"] != ref.get("sha256"):
                        row["warnings"].append("source_hash_mismatch")
                    with Image.open(io.BytesIO(raw)) as image:
                        image.load()
                        width, height = image.size
                        ink = image.convert("L").point(lambda value: 255 if value < 96 else 0)
                        bbox = ink.getbbox()
                    row.update(width=width, height=height, decodable=True)
                    if width < 800:
                        row["warnings"].append("small_width_review_in_preparation")
                    if bbox is None:
                        row["warnings"].append("no_dark_content")
                    else:
                        margins = dict(left=bbox[0], top=bbox[1], right=width-bbox[2], bottom=height-bbox[3])
                        row["ink_margins_px"] = margins
                        # Edge contact is a review hint, not proof of lost text.
                        for edge, margin in margins.items():
                            if margin <= 1:
                                row["warnings"].append("ink_at_" + edge + "_edge")
                except (OSError, ValueError) as exc:
                    row["warnings"].append("unreadable_or_outside_details")
                    row["error"] = str(exc)
                cache[key] = row
            assets.append(cache[key])
        inspected.append(dict(request_id=item.get("request_id"),
                              question_id=item.get("question_id"),
                              true_item_id=selection.get("true_item_id"), assets=assets,
                              warnings=[] if assets else ["no_prepared_image_refs"]))
    images = list(cache.values())
    return dict(schema="cs408-morning-image-preflight-v1", target_date=plan.get("target_date"),
                item_count=len(items), image_count=len(images),
                decodable_count=sum(row["decodable"] for row in images),
                hash_match_count=sum(row.get("sha256") is not None and row["sha256"] == row["expected_sha256"] for row in images),
                warning_image_count=sum(bool(row["warnings"]) for row in images),
                missing_image_item_count=sum(not row["assets"] for row in inspected),
                visual_clarity_verified=False, image_write_count=0, exposure_write_count=0,
                learning_image_policy="display_prepared_links_without_inspection_or_repair",
                items=inspected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--details-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    raw = args.plan.read_bytes()
    report = inspect_plan(json.loads(raw), args.details_root)
    report.update(plan_path=str(args.plan.resolve()), plan_sha256=hashlib.sha256(raw).hexdigest())
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "items"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
