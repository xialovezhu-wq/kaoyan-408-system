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
    const keys = captured ? ["status", "next_action", "next_input_context", "progress",
      "capture_verified", "package_verified", "capture_id", "terminal_state", "replayed",
      "supplemental_complete", "navigation_held", "learning_request_file", "result_file",
      "formal_write_count", "fsrs_write_count", "personal_summary_write_count"]
      : ["status", "next_action", "input_context", "next_input_context", "progress",
      "teaching_context", "grading_reference", "prepared_context", "prepared_visual_context", "write_count",
      "capture_verified", "package_verified", "capture_id", "terminal_state", "replayed",
      "frontier_advanced", "supplemental_complete", "navigation_held", "evidence_source",
      "learning_request_file", "result_file", "formal_write_count", "fsrs_write_count",
      "personal_summary_write_count", "prepared_materials_unavailable", "timing_ms", "timing_scope",
      "startup_recovered", "verification", "material_version"];
    const compact = Object.fromEntries(keys.filter(key => result[key] !== undefined)
      .map(key => [key, result[key]]));
    for (const name of captured ? [] : ["current_display", "next_display"]) {
      if (!result[name]) continue;
      const shownKeys = ["request_id", "true_item_id", "source_formal_id", "concept_id",
        "execution_kind", "source_label", "review_date", "surface_sha256", "receipt_sha256"];
      compact[name] = Object.fromEntries(shownKeys.filter(key => result[name][key] !== undefined)
        .map(key => [key, result[name][key]]));
    }
    if (result.source_message_refs) compact.source_message_count = result.source_message_refs.length;
    // Exact references remain available for a missing/truncated material only.
    if (context && (!["available", "unchanged"].includes(result.prepared_context?.status) ||
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

function sameTeachingDisplay(input, cached) {
  const prior = cached?.input_context;
  return prior && ["handoff", "session_id", "display_receipt", "display_receipt_sha256"]
    .every(key => typeof input?.[key] === "string" && input[key].length > 0 && input[key] === prior[key]);
}

function contextInput(request) {
  const input = {...request.input};
  // v12 callers still own their cache. v13 callers explicitly supply the full
  // material they actually retained, or null after losing their context.
  if (!Object.prototype.hasOwnProperty.call(request, "teaching_cache")) return input;
  const cached = request.teaching_cache;
  const same = sameTeachingDisplay(input, cached);
  const personal = cached?.teaching_context;
  const sameView = personal?.view === (input.view || "protected");
  if (!same || !sameView || personal?.status !== "available" || !personal.version ||
      !Array.isArray(personal.records) || input.known_version !== personal.version) {
    delete input.known_version;
  }
  if (["question_visual_template", "question_exam_introduction"].some(name =>
      personal?.[name]?.status === "unchanged")) delete input.known_version;
  if (!same || !sameView || !cached?.material_version ||
      input.known_material_version !== cached.material_version ||
      cached.prepared_context?.status !== "available" ||
      cached.grading_reference?.status !== "available" ||
      (cached.prepared_visual_context &&
       !["available", "not_prepared"].includes(cached.prepared_visual_context.status))) {
    delete input.known_material_version;
  }
  return input;
}

function mergeTeachingCache(result, cached) {
  const current = {...result};
  const same = sameTeachingDisplay(result.input_context, cached);
  for (const field of ["teaching_context", "grading_reference", "prepared_context", "prepared_visual_context"]) {
    const delta = result[field];
    if (delta?.status !== "unchanged") continue;
    const old = cached?.[field];
    const valid = same && old && (field === "teaching_context"
      ? old.status === "available" && old.version === delta.version && old.view === delta.view && Array.isArray(old.records)
      : cached.material_version === delta.version && result.material_version === delta.version && old.status === "available");
    if (!valid) throw new Error("teaching_cache_missing_or_mismatched");
    current[field] = {...old, ...delta, status: old.status};
    if (field === "teaching_context") {
      current[field].records = old.records;
      current[field].returned = old.records.length;
      // Keep new missing/ambiguous diagnostics while retaining the exact
      // question payload whose version was just revalidated.
      for (const name of ["question_visual_template", "question_exam_introduction"]) {
        if (delta[name]?.status === "unchanged") {
          if (!old[name] || old[name].status === "unchanged") throw new Error("teaching_cache_question_missing");
          current[field][name] = {...old[name], ...delta[name], status: old[name].status};
        }
      }
    }
  }
  return current;
}

return async function runMorningTurn(request) {
  const allowed = new Set([
    "answer-choice", "finalize-reason", "append-turn",
    "finalize-appended-and-next", "finish-item", "close-unresolved", "image-handoff",
    "learning-turn",
    "d-supplement-start", "d-supplement-turn", "d-supplement-context", "d-supplement-image-handoff", "d-supplement-correct-outcome",
  ]);
  if (!request || !allowed.has(request.operation)) {
    throw new Error("unsupported_morning_turn_operation");
  }
  const isD = request.operation.startsWith("d-supplement-");
  const isDContext = request.operation === "d-supplement-context";
  const isDStart = request.operation === "d-supplement-start";
  if (isDContext && request.input && typeof request.input === "object" && !Array.isArray(request.input)) {
    request = {...request, input: contextInput(request)};
  }
  const preparedMarkdown = isD || request.operation === "learning-turn";
  const isImageHandoff = ["image-handoff", "d-supplement-image-handoff"].includes(request.operation);
  const script = isD
    ? "d_supplement_learning_408.py"
    : request.operation === "learning-turn"
    ? "learning_turn_408.py"
    : "morning_campaign_turn_408.py";
  const quote = value => "'" + value.replace(/'/g, "'\\''") + "'";
  const python = '"${CS408_PYTHON:-python3}"';
  const resolver = '"${CODEX_HOME:-$HOME/.codex}/skills/_shared/kaoyan-408/scripts/resolve_408_paths.py"';
  if (request.repo_root !== undefined &&
      (typeof request.repo_root !== "string" || !request.repo_root.startsWith("/"))) {
    throw new Error("absolute_repo_root_required");
  }
  const launch = `${python} ${resolver}${request.repo_root ? ` --repo ${quote(request.repo_root)}` : ""} --run ${quote(script)}`;
  let command;
  if (isImageHandoff) {
    if (typeof request.result_file !== "string" || !request.result_file.startsWith("/")) {
      throw new Error("absolute_result_file_required");
    }
    command = `${launch} ${isD ? "--operation image-handoff" : "image-handoff"} --input-json ${quote(request.result_file)}`;
  } else {
    if (!request.input || typeof request.input !== "object" || Array.isArray(request.input)) {
      throw new Error("dynamic_input_object_required");
    }
    // JSON is one line inside a quoted heredoc: $, backticks and embedded newlines
    // remain data. Only the allowlisted operation is interpolated as shell syntax.
    const body = JSON.stringify(request.input);
    const operation = isD ? (isDStart ? "--operation start" : isDContext ? "--operation context --context-transport" : request.operation === "d-supplement-correct-outcome" ? "--operation correct-outcome" : "--operation turn")
      : request.operation === "learning-turn" ? "" : request.operation;
    command = `${launch} ${operation} --input-json - <<'CS408_TURN_JSON'\n${body}\nCS408_TURN_JSON`;
  }

  const toolStarted = Date.now();
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
  let toolRoundtripMs = Date.now() - toolStarted;
  let result, teachingCache, transport;
  try {
    result = JSON.parse(output);
    if (isDContext && result.transport === "cs408-context-file-v1") {
      transport = result;
      if (typeof transport.path !== "string" || !transport.path.startsWith("/") ||
          !Number.isSafeInteger(transport.characters) || transport.characters < 1 ||
          (transport.sha256 !== undefined && !/^[a-f0-9]{64}$/.test(transport.sha256)) ||
          (transport.bytes !== undefined && (!Number.isSafeInteger(transport.bytes) || transport.bytes < 1))) {
        throw new Error("invalid_context_transport");
      }
      // Verify every read against the producer's byte snapshot. Large or
      // truncated tool outputs fall back to the proven 2,000-character size;
      // the model sees only the fully reassembled, parsed object.
      const program = ["import hashlib,json,pathlib,sys",
        "raw=pathlib.Path(sys.argv[1]).read_bytes()",
        "if sys.argv[2] and hashlib.sha256(raw).hexdigest()!=sys.argv[2]:",
        "    raise ValueError('context_snapshot_hash_mismatch')",
        "if int(sys.argv[3])>=0 and len(raw)!=int(sys.argv[3]):",
        "    raise ValueError('context_snapshot_size_mismatch')",
        "s=raw.decode('utf-8')",
        "if len(s)!=int(sys.argv[4]): raise ValueError('context_snapshot_length_mismatch')",
        "offset=int(sys.argv[5]); count=int(sys.argv[6])",
        "print(json.dumps(s if count<0 else s[offset:offset+count], ensure_ascii=False))"].join("\n");
      async function readPart(offset, count) {
        const chunk = await tools.exec_command({
          cmd: `${python} -c ${quote(program)} ${quote(transport.path)} ${quote(transport.sha256 || "")} ${transport.bytes ?? -1} ${transport.characters} ${offset} ${count}`,
          max_output_tokens: count < 0 ? 32000 : 8000, yield_time_ms: 30000,
        });
        if (chunk.exit_code !== 0) throw new Error("context_chunk_unavailable");
        let part;
        try { part = JSON.parse(chunk.output); } catch (_) { return null; }
        const expected = count < 0 ? transport.characters : Math.min(count, transport.characters - offset);
        return typeof part === "string" && Array.from(part).length === expected ? part : null;
      }
      let encoded = null;
      if (transport.characters <= 48000 && (transport.bytes ?? 0) <= 120000) {
        encoded = await readPart(0, -1);
      }
      if (encoded === null) {
        const parts = [];
        for (let start = 0; start < transport.characters; start += 8000) {
          const offsets = [0, 2000, 4000, 6000].map(n => start + n).filter(n => n < transport.characters);
          const batch = await Promise.allSettled(offsets.map(offset => readPart(offset, 2000)));
          for (const item of batch) {
            if (item.status !== "fulfilled" || item.value === null) throw new Error("context_chunk_incomplete");
            parts.push(item.value);
          }
        }
        encoded = parts.join("");
      }
      if (Array.from(encoded).length !== transport.characters) throw new Error("context_transport_incomplete");
      result = JSON.parse(encoded);
      await tools.exec_command({
        cmd: `${python} -c ${quote("import pathlib,sys; pathlib.Path(sys.argv[1]).unlink()")} ${quote(transport.path)}`,
        max_output_tokens: 1000, yield_time_ms: 30000,
      });
    }
    if (isDContext && result.status === "ready_for_teaching" &&
        Object.prototype.hasOwnProperty.call(request, "teaching_cache")) {
      teachingCache = mergeTeachingCache(result, request.teaching_cache);
    }
  } catch (_error) {
    const failure = {
      status: "runner_result_unavailable", exit_code: execution.exit_code,
      reason: String(_error.message || _error),
      ...(transport ? {context_transport_path: transport.path} : {}),
      recovery: isDContext ? "Retry context with the same display identity; clear lost cache versions. Never submit a learning turn to recover a read."
        : "Replay the same request identity; never invent a new key.",
    };
    text(failure);
    return {result: failure, images_loaded: 0};
  }
  toolRoundtripMs = Date.now() - toolStarted;
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
      let presentation = value.presentation_markdown;
      const progress = result.progress;
      if (presentation && Number.isInteger(progress?.position) &&
          Number.isInteger(progress?.total) && progress.position > 0 &&
          progress.position <= progress.total) {
        const shown = result.next_display || result.current_display;
        const formal = shown?.execution_kind === "original_review" &&
          /^(DS|CO|OS|CN)_(\d{4}|UNK)_\d{3}$/.test(shown?.source_formal_id || "")
          ? `\n\n正式节点：${shown.source_formal_id}` : "";
        presentation = `第 ${progress.position}/${progress.total} 题${formal}\n\n${presentation}`;
      }
      Object.assign(delivery, {image_delivery: value.image_delivery,
        image_inspection_required: false, presentation_markdown: presentation,
        tool_roundtrip_ms: toolRoundtripMs});
    }
    // Emit this only after every post-save tool has returned to this invocation.
    text({one_call_delivery: delivery});
    return {...value, ...(teachingCache ? {teaching_cache: teachingCache} : {}), one_call_delivery: delivery};
  }
  if (preparedMarkdown) {
    const verified = execution.exit_code === 0 && result.status !== "blocked";
    const mayPresent = verified && result.next_action === "present_next" &&
      result.frontier_advanced === true && result.next_display &&
      result.capture_verified === true && result.package_verified === true;
    const current = (isDContext || isDStart) && verified && result.status === "ready_for_teaching";
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
