"""Read authoritative local D sources without building or opening a replica."""
from __future__ import annotations

import csv
import json
from pathlib import Path
import re
import sys

import runtime_paths_408 as paths
import study_bridge_408 as bridge
if str(paths.bridge_root()) not in sys.path:
    sys.path.insert(0, str(paths.bridge_root()))
from bridge_archives import file_hash, require, safe_path, source_file
from exam_practice_units_408 import CORPUS_REL, UNIT_REL, load_units, published_units

SOURCE_BASIS = "native_due_sources_v1"
CATALOG = CORPUS_REL / "indexes/question-index.csv"
POINT_LINKS = "support/syllabus-2027/point-question-links.json"
PERSONAL_PREFIX = "personalization/408/"
CONTRACTS = {
    "support/d408/asset-delivery-contract.md": "schema/d-asset-delivery-408-v1.md",
    "support/d408/web-execution-report-contract.md": "schema/web-execution-report-408-v1.md",
    "support/d408/knowledge-profile-contract.md": "schema/408-knowledge-profile-v1.md",
    "support/d408/question-visual-contract.md": "schema/question-visual-408-v1.md",
    "support/d408/source-visual-contract.md": "schema/source-visual-408-v1.md",
}


def exam_catalog(details):
    with source_file(details / CATALOG, details).open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    catalog = {row["exam_question_id"]: row for row in rows}
    require(len(catalog) == len(rows) and catalog, "D_native_exam_catalog_invalid")
    return catalog


def details_for(repo):
    return paths.details_root() if Path(repo).resolve() == bridge.REPO else Path(repo).resolve().parent / "details"


def contact_proof(repo, day, details):
    from morning_question_policy_408 import prepare_hot_capture_contact_proof

    identities = exam_catalog(details).keys() | load_units(details).keys()
    return prepare_hot_capture_contact_proof(repo, day, native_original_ids=identities)


def exact_ids_for_node(details, node, formal_id):
    row = node["row"]
    joined = bridge.canonical_formal_ids({formal_id: row}, exam_catalog(details).values())
    return set(node["exact_sources"]) | {identity for identity, ids in joined.items() if formal_id in ids}


class NativeMorningSources:
    is_native = True

    def __init__(self, repo, details, *, release_dir=None):
        from exam_tutoring_context_408 import DEFAULT_STATE_REL
        from intake_lib_408 import Repo
        from start_morning_coverage_campaign_408 import DEFAULT_RELEASE
        from study_personalization_408 import current_identity

        self.repo, self.details = Path(repo).resolve(), Path(details).resolve()
        self.teaching_root = self.repo / DEFAULT_STATE_REL
        self.release_dir = (release_dir or self.details / CORPUS_REL / "style-releases" / DEFAULT_RELEASE.name).resolve()
        require(self.release_dir.parent == (self.details / CORPUS_REL / "style-releases").resolve(),
                "D_native_release_outside_managed_corpus")
        self.identity = current_identity(self.repo)
        require(self.identity.get("status") == "ready", "D_native_teaching_not_current", self.identity.get("reasons"))
        self.inputs = {
            path: file_hash(source_file(path))
            for path in (self.details / CATALOG, self.repo / POINT_LINKS, self.release_dir / "manifest.json")
        }
        unit_index = self.details / UNIT_REL / "index.json"
        self.inputs[unit_index] = file_hash(source_file(unit_index)) if unit_index.exists() else None
        self.version = bridge._digest({"basis": SOURCE_BASIS, "teaching": self.identity["version"],
            "formal": self.identity["formal_version"], "catalog": self.inputs[self.details / CATALOG],
            "point_links": self.inputs[self.repo / POINT_LINKS], "release": self.inputs[self.release_dir / "manifest.json"],
            "practice_units": self.inputs[unit_index]})
        self.master = Repo(self.repo).master()
        self.catalog = exam_catalog(self.details)
        self.files = {}
        self.question_refs = {
            fid: {"formal_id": fid, "source_id": row.get("来源ID"),
                  "safe_card": fid + ".md", "detail_card": f"details/cards/{fid}.md",
                  "canonical_ids": [], "main_knowledge_points": bridge._concepts(row)}
            for fid, row in self.master.items()
        }
        links = json.loads(source_file(self.repo / POINT_LINKS, self.repo).read_bytes())["questions"]
        formal_ids = bridge.canonical_formal_ids(self.master, self.catalog.values())
        self.mappings = {}
        for qid, row in self.catalog.items():
            ids = formal_ids[qid]
            self.mappings[qid] = {"question_id": qid, "formal_ids": ids,
                "year": row.get("year"), "official_question_no": row.get("official_question_no"),
                "question_images": self.corpus_paths(json.loads(row["question_assets"] or "[]")),
                "solution_images": self.corpus_paths(json.loads(row["solution_assets"] or "[]")),
                "exam_point_context": links.get(qid, {})}
            for fid in ids:
                if fid in self.question_refs:
                    self.question_refs[fid]["canonical_ids"].append(qid)
        for unit in published_units(self.details):
            qid = unit["question_id"]
            ids = [fid for fid, row in self.master.items() if row.get("来源ID") == qid]
            mapping = {**self.mappings.get(qid, {}), **unit, "formal_ids": ids}
            for name in ("question_images", "solution_images"):
                mapping[name] = self.corpus_paths(unit[name])
            for name in ("card_path", "payload_path"):
                mapping[name] = self.corpus_paths([unit[name]])[0]
            self.mappings[qid] = mapping
            if "-P" not in qid:
                for fid in ids:
                    self.question_refs[fid]["canonical_ids"].append(qid)
        self.verify_current()

    @staticmethod
    def corpus_paths(names):
        result = ["details/" + safe_path(name) for name in names]
        require(all(name.startswith("details/" + CORPUS_REL.as_posix() + "/") for name in result),
                "D_native_asset_outside_exam_corpus")
        return result

    def verify_current(self):
        from study_personalization_408 import current_identity

        current = current_identity(self.repo)
        require(current.get("status") == "ready" and
                all(current.get(k) == self.identity[k] for k in ("source_version", "version", "formal_version")),
                "D_native_teaching_version_changed")
        for path, sha in self.inputs.items():
            actual = file_hash(source_file(path)) if path.exists() else None
            require(actual == sha, "D_native_source_changed", path.name)

    def _location(self, name):
        safe_path(name)
        if name in CONTRACTS:
            return self.repo, self.repo / CONTRACTS[name], [], "local_contract"
        if name.startswith(PERSONAL_PREFIX):
            relative = name.removeprefix(PERSONAL_PREFIX)
            require(re.fullmatch(r"objects/reviewed-knowledge-profiles/[a-f0-9]{64}\.json", relative),
                    "D_native_personal_object_outside_scope")
            return self.teaching_root, self.teaching_root / relative, [], "reviewed_local_profile"
        if name.startswith("details/"):
            relative = name.removeprefix("details/")
            if relative.startswith(CORPUS_REL.as_posix() + "/"):
                return self.details, self.details / relative, [], "canonical_exam_material"
            match = re.fullmatch(r"cards/([^/]+)\.md", relative)
            require(match and match[1] in self.master, "D_native_detail_card_outside_scope")
            return self.details, self.details / relative, [match[1]], "formal_detail_card"
        match = re.fullmatch(r"([^/]+)\.md", name)
        require(match and match[1] in self.master, "D_native_source_outside_scope", name)
        return self.repo, self.repo / name, [match[1]], "formal_card"

    def path(self, name):
        root, path, _, _ = self._location(name)
        return source_file(path, root)

    def _row(self, name):
        if name in self.files:
            return self.files[name]
        root, path, ids, kind = self._location(name)
        if not path.exists():
            return None
        path = source_file(path, root)
        row = {"path": name, "sha256": file_hash(path), "bytes": path.stat().st_size,
               "formal_ids": ids, "kind": kind}
        self.files[name] = row
        return row

    def personal_records(self, points):
        from exam_tutoring_context_408 import QUERY_INPUT_SCHEMA, query_context

        result, proofs = {}, []
        version = self.identity["source_version"]
        for start in range(0, len(points), 32):
            batch = points[start:start + 32]
            view = query_context(self.repo, {"schema": QUERY_INPUT_SCHEMA, "stage": "post_answer",
                                 "knowledge_points": batch, "max_profiles": 0},
                                 teaching_view="direct", materials="core")
            require(view.get("source_version") == version and view.get("view") == "direct"
                    and view.get("status") == "available", "D_native_personal_scope_not_current", batch)
            summaries = {row["knowledge_point"]: row for row in view["records"]}
            require(set(summaries) == set(batch) and len(summaries) == len(view["records"]),
                    "D_native_personal_points_incomplete", sorted(set(batch) - set(summaries)))
            for point, summary in summaries.items():
                profile = (summary.get("personal_source") or {}).get("profile_object")
                sources = []
                if profile:
                    name = PERSONAL_PREFIX + profile["ref"]
                    ref = self._row(name)
                    require(ref and ref["sha256"] == profile["sha256"], "D_native_profile_hash_drift", point)
                    sources.append({"path": name, "sha256": profile["sha256"]})
                result[point] = {"source_version": version, "summary": summary, "source_refs": sources}
            proofs.append({"requested_points": batch, "returned_points": list(summaries),
                           "source_version": version, "complete": True})
        return result, proofs

    def import_inventory(self, items, asset_refs):
        """Resolve only sources belonging to the selected exact questions."""
        from exam_practice_units_408 import verified_unit

        points = list(dict.fromkeys(point for item in items for point in item["review_target_concept_ids"]))
        personal, _ = self.personal_records(points)
        names = {}
        for item in items:
            fid = item["question_id"]
            identity = item.get("preferred_true_item_id") or item.get("true_item_id")
            require(fid in self.master and identity in self.mappings, "D_native_item_source_missing")
            mapping = self.mappings[identity]
            if mapping.get("payload_path"):
                verified_unit(self.details, identity)
            allowed = {fid + ".md", f"details/cards/{fid}.md", "details/" + CATALOG.as_posix(),
                       *mapping["question_images"], *mapping["solution_images"]}
            for point in item["review_target_concept_ids"]:
                allowed.update(ref["path"] for ref in personal[point]["source_refs"])
            for ref in item["source_refs"]:
                require(isinstance(ref, dict) and ref.get("path") in allowed,
                        "D_native_source_ref_outside_item", identity)
                row = self._row(ref["path"])
                require(row and row["sha256"] == ref.get("sha256"), "D_native_source_ref_hash_mismatch")
                names[row["path"]] = row
        for ref in asset_refs:
            require(ref["source_path"] in names, "D_native_asset_source_not_declared", ref["source_path"])
        for row in names.values():
            require(file_hash(self.path(row["path"])) == row["sha256"],
                    "D_native_import_material_changed", row["path"])
        return {"subject": "408", "source_basis": SOURCE_BASIS, "local_version": self.version,
                "question_refs": self.question_refs, "original_mappings": list(self.mappings.values()),
                "files": list(names.values())}
