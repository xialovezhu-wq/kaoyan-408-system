/* Trusted Function body for functions.exec; parameters: tools, text, image.
 * Returns an async function accepting {operation, input} or image-handoff result_file.
 * It never evaluates learner text, question content, or paths as JavaScript.
 */
// Full receipts stay in result_file and in the returned JS object. Emit only
// the fields needed for the next teaching decision on healthy turns.
function teachingResult(result, exitCode, operation) {
  const isD = operation?.startsWith("d-supplement-");
  const context = result.status === "ready_for_teaching";
  const captured = result.status === "episode_captured" &&
    result.capture_verified === true && result.package_verified === true &&
    ["present_next", "hold"].includes(result.next_action);
  if (isD && exitCode === 0 && (context || captured) &&
      result.image_preload_status !== "blocked") {
    const keys = ["status", "next_action", "input_context", "next_input_context", "progress",
      "teaching_context", "grading_reference", "prepared_context", "write_count",
      "capture_verified", "package_verified", "capture_id", "terminal_state", "replayed",
      "frontier_advanced", "supplemental_complete", "navigation_held", "evidence_source",
      "learning_request_file", "result_file", "formal_write_count", "fsrs_write_count",
      "personal_summary_write_count", "prepared_materials_unavailable"];
    const compact = Object.fromEntries(keys.filter(key => result[key] !== undefined)
      .map(key => [key, result[key]]));
    for (const name of ["current_display", "next_display"]) {
      if (!result[name]) continue;
      const shownKeys = ["request_id", "true_item_id", "source_formal_id", "concept_id",
        "execution_kind", "source_label", "review_date", "surface_sha256", "receipt_sha256"];
      compact[name] = Object.fromEntries(shownKeys.filter(key => result[name][key] !== undefined)
        .map(key => [key, result[name][key]]));
    }
    if (result.source_message_refs) compact.source_message_count = result.source_message_refs.length;
    // Exact references remain available for a missing/truncated material only.
    if (context && (result.prepared_context?.status !== "available" ||
        result.prepared_context?.solution?.truncated || result.prepared_context?.teaching?.truncated)) {
      compact.prepared_material_refs = result.prepared_material_refs;
    }
    return compact;
  }
  // Do not hide any diagnostic or recovery data for D failures.
  if (isD) return result;
  const healthy = new Set([
    "buffered", "failure_pending_capture", "failure_finalized", "complete",
    "screened_correct", "reason_required", "reason_audited",
    "episode_pending_terminal", "episode_buffered", "episode_captured", "episode_captured_held", "episode_finalized",
  ]);
  if (exitCode !== 0 || !healthy.has(result.status) ||
      result.image_preload_status === "blocked") return result;
  const keys = [
    "status", "run_id", "item_id", "choice_result", "rating", "reason_required",
    "capture_required", "capture_verified", "correction_review_commit_verified",
    "frontier_advanced", "campaign_complete", "concept_coverage_complete",
    "unanswered_node_count", "completion_message", "replayed", "formal_write_count",
    "turn_count", "trace_sha256", "request_file", "result_file",
    "light_evidence_saved", "learning_turn_count",
    "episode", "terminal_state", "package_verified", "capture_id", "capture_receipt_ref",
    "capture_receipt_sha256", "conversation_package_locator", "conversation_package_sha256",
    "progress", "navigation_held", "fsrs_write_count", "personal_summary_write_count",
    "saving_mode", "episode_capture_every_item", "saving_contract_sha256",
    "image_preload_status", "next_image_paths", "next_display",
    "next_action", "celebration_eligible", "learning_request_file",
    "current_display_receipt_sha256", "evidence_source", "source_message_refs",
    "next_input_context", "supplemental_complete", "prepared_materials_unavailable",
    "image_delivery", "image_inspection_required",
  ];
  const compact = Object.fromEntries(keys.filter(key => result[key] !== undefined)
    .map(key => [key, result[key]]));
  if (result.current_item_personalization) {
    // Keep the bounded evidence and freshness fields; omit its duplicate prose.
    const {local_records_markdown, ...personalization} = result.current_item_personalization;
    compact.current_item_personalization = personalization.status === "available"
      ? personalization : result.current_item_personalization;
  }
  if (result.study_followup) {
    const followup = result.study_followup;
    compact.followup = {
      local_status: followup.local_tutoring?.status,
      source_version: followup.local_tutoring?.source_version,
      publication_status: followup.publication?.status,
    };
    // Preserve full diagnostic evidence whenever the follow-up is not healthy.
    if (followup.local_tutoring &&
        !["refreshed", "already_current"].includes(followup.local_tutoring.status)) {
      compact.study_followup = followup;
    }
    if (followup.publication &&
        !["STARTED", "PUBLISHED", "ALREADY_PUBLISHED", "SKIPPED"].includes(followup.publication.status)) {
      compact.study_followup = followup;
    }
  }
  return compact;
}

return async function runMorningTurn(request) {
  const allowed = new Set([
    "answer-choice", "finalize-reason", "append-turn",
    "finalize-appended-and-next", "finish-item", "close-unresolved", "image-handoff",
    "learning-turn",
    "d-supplement-turn", "d-supplement-context", "d-supplement-image-handoff",
  ]);
  if (!request || !allowed.has(request.operation)) {
    throw new Error("unsupported_morning_turn_operation");
  }
  const isD = request.operation.startsWith("d-supplement-");
  const isDContext = request.operation === "d-supplement-context";
  const preparedMarkdown = isD || request.operation === "learning-turn";
  const isImageHandoff = ["image-handoff", "d-supplement-image-handoff"].includes(request.operation);
  const script = isD
    ? "/Users/your-user/Documents/kaoyan-408/scripts/d_supplement_learning_408.py"
    : request.operation === "learning-turn"
    ? "/Users/your-user/Documents/kaoyan-408/scripts/learning_turn_408.py"
    : "/Users/your-user/Documents/kaoyan-408/scripts/morning_campaign_turn_408.py";
  const quote = value => "'" + value.replace(/'/g, "'\\''") + "'";
  let command;
  if (isImageHandoff) {
    if (typeof request.result_file !== "string" || !request.result_file.startsWith("/")) {
      throw new Error("absolute_result_file_required");
    }
    command = `/usr/local/bin/python3 ${quote(script)} ${isD ? "--operation image-handoff" : "image-handoff"} --input-json ${quote(request.result_file)}`;
  } else {
    if (!request.input || typeof request.input !== "object" || Array.isArray(request.input)) {
      throw new Error("dynamic_input_object_required");
    }
    // JSON is one line inside a quoted heredoc: $, backticks and embedded newlines
    // remain data. Only the allowlisted operation is interpolated as shell syntax.
    const body = JSON.stringify(request.input);
    const operation = isD ? (isDContext ? "--operation context" : "--operation turn")
      : request.operation === "learning-turn" ? "" : request.operation;
    command = `/usr/local/bin/python3 ${quote(script)} ${operation} --input-json - <<'CS408_TURN_JSON'\n${body}\nCS408_TURN_JSON`;
  }

  let execution = await tools.exec_command({
    cmd: command, max_output_tokens: 8000, yield_time_ms: 30000,
  });
  let output = execution.output || "";
  while (execution.session_id !== undefined && execution.session_id !== null) {
    execution = await tools.write_stdin({
      session_id: execution.session_id, chars: "", max_output_tokens: 8000,
      yield_time_ms: 1000,
    });
    output += execution.output || "";
  }
  let result;
  try {
    result = JSON.parse(output);
  } catch (_error) {
    const failure = {
      status: "runner_result_unavailable", exit_code: execution.exit_code,
      recovery: "Replay the same request identity; never invent a new key.",
    };
    text(failure);
    return {result: failure, images_loaded: 0};
  }
  text(teachingResult(result, execution.exit_code, request.operation));
  // The caller supplies this optional effect only for an existing celebration
  // preference. It must not require a second model turn or alter saved outcomes.
  const verifiedRepair = result.capture_verified === true &&
    result.correction_review_commit_verified === true &&
    ["failure_finalized", "complete"].includes(result.status);
  const verifiedCorrect = result.choice_result === "correct" &&
    ["Hard", "Good"].includes(result.rating) && result.capture_required !== true;
  const verifiedEpisode = result.capture_verified === true && result.package_verified === true &&
    ["correct", "screened_correct", "prompted_correct"].includes(result.terminal_state);
  const mayCelebrate = !isImageHandoff && !isDContext && execution.exit_code === 0 &&
    result.status !== "blocked" && result.replayed !== true && result.frontier_advanced === true &&
    (!preparedMarkdown || (result.capture_verified === true && result.package_verified === true &&
      result.next_action === "present_next")) &&
    (["learning-turn", "d-supplement-turn"].includes(request.operation) ? result.celebration_eligible === true
      : (verifiedRepair || verifiedCorrect || verifiedEpisode));
  async function finish(value) {
    let celebrationCall = "not_requested";
    if (mayCelebrate && typeof tools.celebrate === "function") {
      try {
        await tools.celebrate();
        celebrationCall = "returned";
      } catch (_) {
        celebrationCall = "failed";
      }
    }
    const blocked = execution.exit_code !== 0 || result.status === "blocked" ||
      (!preparedMarkdown && result.image_preload_status === "blocked") ||
      (preparedMarkdown && result.frontier_advanced === true &&
        (result.capture_verified !== true || result.package_verified !== true)) ||
      (result.capture_required === true && result.capture_verified !== true &&
        result.frontier_advanced === true);
    const delivery = {
      images_loaded: value.images_loaded,
      celebration_call_status: celebrationCall,
      next_action: value.image_preload_failed ? "retry_image_handoff"
        : blocked ? "resolve_blocker"
        : result.frontier_advanced === true && result.next_display &&
          (!preparedMarkdown || value.prepared_presentation === true) &&
          (value.prepared_presentation === true || value.images_loaded > 0 || result.image_preload_status === "not_applicable")
          ? "present_returned_next_display"
        : result.campaign_complete === true || result.supplemental_complete === true ? "report_completion"
        : "continue_current_item",
    };
    if (value.image_delivery === "prepared_markdown") {
      Object.assign(delivery, {image_delivery: value.image_delivery,
        image_inspection_required: false, presentation_markdown: value.presentation_markdown});
    }
    // Emit this only after every post-save tool has returned to this invocation.
    text({one_call_delivery: delivery});
    return {...value, one_call_delivery: delivery};
  }
  if (preparedMarkdown) {
    const verified = execution.exit_code === 0 && result.status !== "blocked";
    const mayPresent = verified && result.next_action === "present_next" &&
      result.frontier_advanced === true && result.next_display &&
      result.capture_verified === true && result.package_verified === true;
    const current = isDContext && verified && result.status === "ready_for_teaching";
    const paths = mayPresent ? result.next_image_paths : current ? result.current_image_paths : [];
    const shown = mayPresent ? result.next_display : current ? result.current_display : null;
    // Preparation owns pixels. Return references for the chat renderer; never
    // reread, crop or repair images during a learner turn.
    // An answer query already has this question on screen. Protected recovery
    // and verified successor delivery still return every prepared image link.
    const repeatPresentation = !isDContext || !["after_attempt", "direct"].includes(request.input?.view);
    const presentation = !repeatPresentation ? "" : shown?.presentation_markdown || (paths || [])
      .filter(path => typeof path === "string" && path.startsWith("/"))
      .map((path, i) => `![题目 ${i + 1}](${path})`).join("\n\n");
    return finish({result, images_loaded: 0, prepared_presentation: Boolean(mayPresent),
      image_delivery: "prepared_markdown", presentation_markdown: presentation});
  }
  const contextImages = isDContext && result.status === "ready_for_teaching"
    ? result.current_image_paths : null;
  if (!contextImages && (execution.exit_code !== 0 || result.status === "blocked" ||
      result.image_preload_status !== "ready" || result.frontier_advanced !== true ||
      !result.next_display ||
      (result.capture_required === true && result.capture_verified !== true))) {
    return finish({result, images_loaded: 0});
  }
  const paths = contextImages || result.next_image_paths;
  if (contextImages?.length === 0) return finish({result, images_loaded: 0});
  if (!Array.isArray(paths) || paths.length === 0 ||
      paths.some(path => typeof path !== "string" || !path.startsWith("/"))) {
    throw new Error("verified_next_image_paths_missing");
  }
  const loaded = await Promise.allSettled(paths.map(path => tools.view_image({path})));
  if (loaded.some(entry => entry.status === "rejected" || !entry.value?.image_url)) {
    text({
      image_preload_status: "blocked", reason: "image_tool_read_failed",
      result_file: result.result_file,
      recovery: "Use image-handoff on this result file; do not resubmit the learner turn.",
    });
    return finish({result, images_loaded: 0, image_preload_failed: true});
  }
  for (const entry of loaded) image(entry.value.image_url);
  return finish({result, images_loaded: loaded.length});
};
