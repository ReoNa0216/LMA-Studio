"""Exercise the real Windows WebView2 review UI on a disposable MPP project copy.

The supplied project must live under this repository's build directory. Three
known MPP relations are reset in that copy only. No JavaScript action, confirm,
fetch, render or backend function is mocked. Native dialogs are recorded and
rejected so an unexpected dialog fails the test instead of hanging the runner.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import threading
import time
import traceback

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

IDS = ['manual_cell:36a4d4f21c', 'manual_cell:8c786970b0', 'manual_cell:811e8247c8']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-dir', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    project = args.project_dir.resolve()
    if sys.platform != 'win32' or not project.is_relative_to(ROOT / 'build'):
        parser.error('Windows only; use a disposable project copy under lma-studio/build')

    import webview
    from annotation_app.app import AppData, ProjectPaths
    from annotation_app.desktop import DesktopApi, DesktopServer

    paths = ProjectPaths.from_args(project_dir=str(project),
                                   annotation_db=str(project / 'annotations/annotation.sqlite'))
    app = AppData.load(paths)
    baseline = {r['annotation_id']: r for r in app.store.records()}
    for key in IDS:
        row = app.store.get(key)
        assert row and app.annotation_review_stage(row) == 'cell_annotation'
        app.store.upsert_review(annotation_id=key, source=row['source'],
                               review_status='pending', payload=row,
                               action='webview_test_fixture', notes='Disposable integration-test copy only')
    server = DesktopServer(app)
    api = DesktopApi(server, webview)
    window = webview.create_window('LMA review integration test', server.webview_url,
                                   js_api=api, width=1280, height=900, hidden=True)
    loaded = threading.Event()
    window.events.loaded += lambda: loaded.set()
    report = {'project': str(project), 'dialogs': []}

    def wait_js(expression, timeout=25):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = window.evaluate_js(expression)
            if value:
                return value
            time.sleep(.15)
        raise RuntimeError('Timed out: ' + expression)

    def view():
        return window.evaluate_js("""({
          button:el('acceptWindow').textContent,
          visible:el('acceptWindow').getClientRects().length > 0,
          disabled:el('acceptWindow').disabled,
          hint:el('interactionHint').textContent,
          pending:candidateRows().filter(r=>r.review_status==='pending').map(rowId),
          lines:[...document.querySelectorAll('line')]
            .filter(e=>e.__detail?.kind==='manual_cell' && e.getAttribute('opacity')!=='0')
            .map(e=>({id:e.__detail.data.annotation_id,status:e.__detail.data.review_status,
                      dash:e.getAttribute('stroke-dasharray')})),
          errors:window.__reviewTestErrors
        })""")

    def exercise():
        detach_dialog = None
        try:
            assert loaded.wait(25)
            wait_js('Boolean(state.meta && state.current)')
            from System import Action

            def dialog(sender, event):
                report['dialogs'].append({'kind': str(event.Kind), 'message': event.Message})

            def attach():
                core = window.native.webview.CoreWebView2
                core.Settings.AreDefaultScriptDialogsEnabled = False
                core.ScriptDialogOpening += dialog

            def detach_dialog():
                core = window.native.webview.CoreWebView2
                core.ScriptDialogOpening -= dialog
                core.Settings.AreDefaultScriptDialogsEnabled = True

            window.native.Invoke(Action(attach))
            window.evaluate_js("""
              window.__reviewTestErrors=[];
              addEventListener('error', e=>__reviewTestErrors.push(e.message));
              addEventListener('unhandledrejection', e=>__reviewTestErrors.push(String(e.reason)));
              state.stage='event_annotation'; state.start=55; state.width=1;
              state.timeMode='aligned'; window.__reviewReady=false;
              loadWindow().then(()=>window.__reviewReady=true);
            """)
            wait_js('window.__reviewReady')
            before = report['before'] = view()
            assert before['visible'] and not before['disabled']
            assert set(before['pending']) == set(IDS)
            dashed = [r['id'] for r in before['lines'] if r['status'] == 'pending' and r['dash']]
            assert sorted(dashed) == sorted(IDS), dashed

            window.evaluate_js("document.querySelector('[data-action=\"rejected\"][data-id=\""
                               + IDS[0] + "\"]').click(); true")
            wait_js('!state.actionBusy && candidateRows().filter(r=>r.review_status===\'pending\').length===2')
            report['after_reject'] = view()
            assert report['after_reject']['button'] == '接受本屏待审（2）'
            assert not report['after_reject']['disabled'], 'Single review left batch button disabled'
            window.evaluate_js("el('acceptWindow').click(); true")
            wait_js('!state.actionBusy && candidateRows().filter(r=>r.review_status===\'pending\').length===0')
            after = report['after_accept'] = view()
            assert after['disabled'] and '已接受 2 条' in after['hint']
            assert not after['errors'] and not report['dialogs']
            assert app.store.get(IDS[0])['review_status'] == 'rejected'
            assert all(app.store.get(key)['review_status'] == 'accepted' for key in IDS[1:])
            for key in IDS[1:]:
                line = next(r for r in after['lines'] if r['id'] == key)
                assert line['status'] == 'accepted' and not line['dash']
            after_records = {r['annotation_id']: r for r in app.store.records()}
            assert baseline.keys() == after_records.keys()
            assert all(baseline[key] == after_records[key] for key in baseline if key not in IDS)
            reopened = AppData.load(paths)
            assert [reopened.store.get(key)['review_status'] for key in IDS] == ['rejected','accepted','accepted']
            report.update(ok=True, reopened=True, other_decisions_unchanged=True)
        except BaseException:
            report.update(ok=False, error=traceback.format_exc())
        finally:
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
            if detach_dialog is not None:
                window.native.Invoke(Action(detach_dialog))
            window.destroy()

    server.start()
    try:
        webview.start(exercise, gui='edgechromium', private_mode=True)
    finally:
        server.stop()
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
