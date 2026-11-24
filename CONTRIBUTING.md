# Contributing

Issues and pull requests are welcome, especially support for other boards, chassis layouts and BMCs.

- **Keep it dependency-free:** Python standard library only in `fanctl/app`, and plain HTML/JS in `static/index.html`.
- **Safety first.** Anything that touches fan duty must fail toward *more* airflow. Changes to `compute()`,
  `check_emergency()`, `validate_config()` or the host watchdog need a test in `tests/`.
- Run the checks locally:
  ```sh
  python3 tests/test_fanctl.py
  cd fanctl/app && python3 fanctl.py --mock --data /tmp/mock --port 18090
  ```
- **Screenshots must come from mock mode.** Never post real drive serials, BMC addresses or `edit_token` values in issues.
- Hardware reports are useful: board, BMC firmware, which zone drives which header, and `ipmitool sensor` output
  (redact serials).
