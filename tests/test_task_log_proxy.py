from io import StringIO

from check_and_clean_accounts import _ProbeProgress, _ProgressReporter


def test_cli_logs_full_proxy_only_during_detection():
    proxy = "http://user:secret@proxy.example.test:8080"
    output = StringIO()
    reporter = _ProgressReporter(output)

    reporter.emit(1, None, {
        "stage": "代理检测", "status": "开始", "proxy": proxy, "proxy_index": 7,
    })
    reporter.emit(1, "account@example.test", {
        "stage": "账号检查", "status": "成功", "proxy": proxy, "proxy_index": 7,
    })

    detection, account = output.getvalue().splitlines()
    assert f"proxy={proxy}" in detection
    assert "proxy_index" not in detection
    assert "proxy=7" not in detection
    assert "proxy=" not in account
    assert "proxy_index" not in account


def test_proxy_detection_events_include_original_proxy_url():
    proxy = "https://user:secret@proxy.example.test:8443"

    class Reporter:
        def __init__(self):
            self.events = []
            self.verbose = False

        def emit(self, worker, email, event):
            self.events.append(event)

    reporter = Reporter()
    progress = _ProbeProgress(reporter, 1)

    assert progress.check(
        (4, proxy),
        lambda _proxy: None,
        lambda _proxy: {"ip": "127.0.0.1", "country_code": "LO", "country_name": "Local"},
    )
    detection_events = [event for event in reporter.events if event["stage"] == "代理检测"]
    assert detection_events
    assert all(event["proxy"] == proxy for event in detection_events)
    assert all(event["proxy_index"] == 4 for event in detection_events)
