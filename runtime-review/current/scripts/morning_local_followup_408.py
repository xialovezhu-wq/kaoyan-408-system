"""Current local follow-up for morning study, including older frozen sessions' display only."""

LOCAL_STUDY_FOLLOWUP = (
    '只结束学习时，按原会话规则封存当前题，报告已保存和未完成范围后停止。'
    '用户要求正式入库时，继续 kaoyan-408-enhancement-intake：当前会话读取指定范围的本地Capture、原图与正式摘要，'
    '完成语义编纂和串行写入，用同版本机原生讲题入口验回摘要、版本和答案权限。'
    '只有用户明确要求远程访问或外发才发布。用户要求晚间复盘时，继续 kaoyan-408-evening-review，'
    '按本机当天证据准备所需复盘。两项都已授权时，先正式入库再备晚间复盘。'
)


def terminal_delivery(result, *, restored=False):
    """Add tutor guidance to the live response without changing a receipt or frontier."""
    if result.get('status') == 'blocked':
        return result
    saved = result.get('capture_verified') and result.get('package_verified')
    complete = (result.get('campaign_complete') or result.get('supplemental_complete')
                or result.get('status') in ('learning_complete', 'session_closed'))
    ended = saved and result.get('next_action') == 'hold'
    if not (restored or complete or ended):
        return result
    progress = result.get('progress')
    if progress is None or isinstance(progress, dict):
        # Cached one-call helpers already pass the progress object through.
        return {**result, 'progress': {**(progress or {}), 'tutor_followup_on_request': LOCAL_STUDY_FOLLOWUP}}
    return {**result, 'tutor_followup_on_request': LOCAL_STUDY_FOLLOWUP}
