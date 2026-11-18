"""Prepare an already disclosed 408 figure for the current chat's native HTML tab."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import quote

import runtime_paths_408 as paths
from render_interactive_408 import inline_destination
from visual_selection_408 import from_html


def task_root(root=None):
    thread = os.environ.get('CODEX_THREAD_ID')
    if root is None:
        if not thread or not re.fullmatch(r'[A-Za-z0-9_-]+', thread):
            raise ValueError('sidebar_current_task_directory_required')
        candidates = list((paths.codex_home() / 'visualizations').glob(f'????/??/??/{thread}'))
        if len(candidates) != 1:
            raise ValueError('sidebar_current_task_directory_required')
        root = candidates[0]
    directory = inline_destination(Path(root) / 'sidebar.html', root).parent
    if thread and directory.name != thread:
        raise ValueError('sidebar_directory_belongs_to_another_chat')
    return directory


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def panel_request(path, content_sha, source_sha, title, **details):
    # The host tool accepts right/bottom or its default. The chat dock controls
    # the visible left/right order. A prepared request is not an open receipt.
    url = quote(str(path), safe='/')
    label = title.replace('[', '［').replace(']', '］')
    return dict(status='sidebar_ready', path=str(path), html_sha256=content_sha,
                source_sha256=source_sha, title=title, link_label=label, chat_link=f'[{label}](<{path}>)',
                browser_file_url='file://' + url,
                open_tool='mcp__codex_app__open_in_codex',
                open_arguments={'target': {'type': 'file', 'path': str(path)}},
                layout='visual_left_chat_right', panel_opened=False, learning_write_count=0, **details)


def write_page(data, root, name, source_sha, title, **details):
    directory = task_root(root)
    digest = _sha(data)
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:64].rstrip('-') or '408'
    path = inline_destination(directory / f'{slug}-{digest[:20]}.html', directory)
    directory.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with tempfile.NamedTemporaryFile(dir=directory, prefix='.sidebar-', delete=False) as tmp:
            staging = Path(tmp.name)
            try:
                tmp.write(data)
                tmp.close()
                try:
                    os.link(staging, path)
                except FileExistsError:
                    pass
            finally:
                staging.unlink(missing_ok=True)
    if path.is_symlink() or _sha(path.read_bytes()) != digest:
        raise ValueError('sidebar_page_hash_drift')
    return panel_request(path, digest, source_sha, title, **details)


def prepare(source, expected_sha, *, root=None, title='408 本题图解', display_binding=None):
    """The caller provides the exact learner artifact selected at its permitted stage."""
    source = Path(source).expanduser().absolute()
    if (source.suffix.lower() not in ('.htm', '.html') or not source.is_file()
            or source.is_symlink() or any(p.is_symlink() for p in source.parents)
            or not re.fullmatch(r'[0-9a-f]{64}', expected_sha or '')):
        raise ValueError('exact_learner_html_and_sha_required')
    raw = source.read_bytes()
    if _sha(raw) != expected_sha:
        raise ValueError('sidebar_source_hash_drift')
    doc = raw.decode('utf-8-sig')
    complete = bool(re.match(r'\s*(?:<!doctype\s+html\b|<html\b)', doc, re.I))
    scope = from_html(raw)
    if (not complete and b'data-cs408-selection>' in raw and scope is None
            or display_binding is not None and scope != display_binding):
        raise ValueError('sidebar_selection_binding_mismatch')
    if not title or len(title) > 150:
        raise ValueError('short_public_page_title_required')
    if complete:
        # A complete reading page may use sibling images. Keep its source base
        # and byte identity rather than copying a bare page away from its assets.
        return panel_request(source, expected_sha, expected_sha, title, display_binding=scope)
    if not doc.strip():
        raise ValueError('sidebar_empty_figure')
    head = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="color-scheme" content="light dark"><title>' + html.escape(title) +
            '</title><style>html{font:16px/1.55 system-ui,sans-serif;color:var(--foreground);'
            'background:var(--background);--background:#fff;--foreground:#17263a;'
            '--muted-foreground:#526578;--border:#ccd5df}body{margin:0;padding:clamp(10px,2.8vw,28px);'
            'min-width:0}button,input,select{font:inherit}button{cursor:pointer}'
            ':focus-visible{outline:2px solid #1878a2;outline-offset:2px}'
            '@media(prefers-color-scheme:dark){html{--background:#18232f;--foreground:#ecf2f7;'
            '--muted-foreground:#b2c4d4;--border:#465869}}</style></head><body>\n')
    page = head.encode() + raw + b'\n</body></html>\n'
    return write_page(page, root, source.stem, expected_sha, title, display_binding=scope)


def from_receipt(receipt, *, root=None, title='408 本题图解'):
    if not isinstance(receipt, dict) or receipt.get('status') not in {'rendered', 'sidebar_ready'}:
        raise ValueError('successful_learner_render_receipt_required')
    return prepare(receipt['path'], receipt.get('html_sha256') or receipt.get('sha256'),
                   root=root, title=title, display_binding=receipt.get('display_binding'))


def finish_renderer(python, script, arguments, environment, root):
    """Only the read-only learner-render CLI uses this adapter; keep sealed asset bytes intact."""
    completed = subprocess.run([python, str(script), *arguments], env=environment, stdout=subprocess.PIPE)
    if completed.returncode:
        sys.stdout.buffer.write(completed.stdout)
        return completed.returncode
    result = json.loads(completed.stdout)
    visual = result.get('visual') or result
    if visual.get('status') == 'rendered' and visual.get('inline_path_preflight_passed'):
        title = '408 本题图解'
        identity = (visual.get('display_binding') or {}).get('identity') or visual.get('identity')
        if isinstance(identity, str) and re.fullmatch(r'[A-Za-z0-9#_-]{1,90}', identity):
            title = '408 图解 · ' + identity
        result['sidebar'] = from_receipt(visual, root=root, title=title)
        visual.pop('visualize_reference', None)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def show_in_current_window(page, window_title, link_label, origin_display=None):
    """Open in the originating chat. Its physical screen is not a routing rule."""
    if not window_title or not link_label:
        raise ValueError('exact_chat_title_and_link_label_required')
    helper = paths.codex_home() / 'skills/_shared/kaoyan-408/scripts/show_sidebar.swift'
    if not helper.is_file():
        helper = Path(__file__).resolve().parents[1] / 'codex-skill-sources/_shared/kaoyan-408/scripts/show_sidebar.swift'
    if not helper.is_file():
        return {'status':'host_controls_needed','panel_opened':False,
                'next_action':'verify_file_and_dock_chat_right_in_originating_chat'}
    args = ['swift', str(helper), str(Path(page).resolve()), window_title, link_label]
    if origin_display:
        args.append(origin_display)
    process = subprocess.run(args,
                             capture_output=True, text=True, timeout=25)
    if not process.stdout.strip():
        raise RuntimeError('native_sidebar_readback_unavailable')
    result = json.loads(process.stdout)
    shown = (process.returncode == 0 and result.get('status') == 'shown'
             and result.get('path') == str(Path(page).resolve()) and result.get('page') and result.get('chat'))
    return {**result, 'panel_opened':bool(shown), 'learning_write_count':0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--receipt', help='Selected learner render receipt JSON, or - for stdin')
    mode.add_argument('--html', type=Path, help='Exact already authorized learner HTML')
    mode.add_argument('--spatial-context', help='Current 408 teaching decision JSON, or - for stdin')
    mode.add_argument('--render', nargs=argparse.REMAINDER,
                      help='Run an existing learner renderer with its ordinary arguments and --inline-root')
    mode.add_argument('--show-page', type=Path, help='Open/verify the exact HTML in the originating chat')
    parser.add_argument('--window-title')
    parser.add_argument('--link-label')
    parser.add_argument('--origin-display', help='Disambiguate duplicate windows already showing the same chat')
    parser.add_argument('--sha256')
    parser.add_argument('--root', type=Path)
    parser.add_argument('--title', default='408 本题图解')
    args = parser.parse_args()
    try:
        if args.render is not None:
            allowed = {'render_interactive_408.py': None,
                       'source_visual_408.py': 'render', 'exam_year_teaching_408.py': 'use',
                       'local_quality_408.py': 'render-topic'}
            if not args.render or args.render[0] not in allowed:
                raise ValueError('approved_408_renderer_required')
            name, *arguments = args.render
            verb = allowed[name]
            if verb and arguments[:1] != [verb]:
                raise ValueError('read_only_learner_render_operation_required')
            check = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
            check.add_argument('--inline-root', required=True)
            options, _ = check.parse_known_args(arguments)
            return finish_renderer(sys.executable, paths.repo_root() / 'scripts' / name,
                                   arguments, os.environ, options.inline_root)
        elif args.show_page:
            result = show_in_current_window(args.show_page, args.window_title, args.link_label, args.origin_display)
        elif args.html:
            result = prepare(args.html, args.sha256, root=args.root, title=args.title)
        else:
            source = args.receipt or args.spatial_context
            value = json.load(sys.stdin) if source == '-' else json.loads(Path(source).read_bytes())
            if args.receipt is not None:
                result = from_receipt(value, root=args.root, title=args.title)
            else:
                from sidebar_spatial_408 import prepare_context
                result = prepare_context(value, root=args.root)
    except (KeyError, OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({'status':'sidebar_unavailable','error':str(exc),'panel_opened':False,
                          'learning_write_count':0}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 2 if args.show_page and not result.get('panel_opened') else 0


if __name__ == '__main__':
    sys.exit(main())
