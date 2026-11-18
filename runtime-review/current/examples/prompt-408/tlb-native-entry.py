# Exact function excerpts from scripts/render_interactive_408.py for review. Dependencies run in the native 408 project.

def render_tlb_comparison(output, *, inline_root=None, context=None):
    """Render the scoped, independent comparison on the installed 408 renderer."""
    from question_diagram_408 import render as render_diagram, tlb_comparison
    from visual_selection_408 import from_html

    identity, version = "DEMO-TLB-MISS-VS-PAGE-FAULT", "tlb-miss-vs-page-fault-v1"
    if context is not None:
        scope = context.get("question_scope") or {}
        receipt = context.get("personal_read_receipt") or {}
        identity, version = scope.get("identity"), context.get("source_version")
        require(context.get("schema") == "knowledge_teaching_context_408_v1"
                and context.get("status") == "available" and receipt.get("view") == "direct"
                and isinstance(identity,str) and re.fullmatch(r"EXAM408-\d{4}-Q\d{2}(?:-P\d{2})?",identity)
                and isinstance(version,str) and re.fullmatch(r"[a-f0-9]{64}",version)
                and version == receipt.get("source_version"), "tlb_comparison_requires_current_direct_scope")
        from study_personalization_408 import current_identity
        require(current_identity(_paths.repo_root()).get("source_version") == version,
                "tlb_comparison_source_version_changed")
    result = render_diagram(tlb_comparison(), output, identity=identity, aspect="scoped_mechanism_example",
        inline_root=inline_root, scope=("direct",5,version), release="direct-L5",
        compact={"title":"TLB 未命中与缺页", "key_condition":"地址已合法映射、访问权限满足、页表可查。缺页分支假设可以恢复。"})
    return result | {"display_binding": from_html(Path(result["path"]).read_bytes()),
                     "example_kind": "public_mechanism", "selection_state_status": "last_state_unknown"}
