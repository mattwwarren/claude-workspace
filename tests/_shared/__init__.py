"""Shared test infrastructure, split out of ``tests/conftest.py`` (#2668).

Each module owns one concern. Fixture-bearing modules are registered through
``pytest_plugins`` in ``tests/conftest.py``; helpers are imported explicitly
(``from tests._shared.<module> import name``). New shared helpers go in the
concern module here, never into ``tests/conftest.py``. Every module stays under
the ~1000-line ceiling (pinned by ``tests/test_shared_layer.py``).

Concern -> module:

- repo paths, ``src/`` walk, workflow/command prose and fence readers
  (``_REPO_ROOT``, ``_cmd``, ``_bash_fences``): ``workflow_text``
- guard-script fences and ``cw-script-version`` marker cases
  (``run_guard_fence``, ``GUARD_MARKER_*``): ``guard_fences``
- fake ``~/.claude`` skill/command/context files (``write_skill``):
  ``claude_home_files``
- Step 2.5 gate key, claimants and gate fences (``gate_key_for``,
  ``clean_gate_env``, ``sleeper``): ``step25_gates`` (plugin)
- seeded sessions, ticket tasks, tick summaries (``_seed_sessions``,
  ``_make_daemon_session``): ``session_seeding``
- stop-hook transcripts and hook-context files (``_write_hook_context_file``):
  ``stop_hook_context``
- flock holders, fake lock clock, lock-hold records (``fake_lock_clock``,
  ``lock_hold_clock``): ``lock_helpers`` (plugin)
- review findings, constraints, debt records, diffs (``_make_finding``,
  ``_make_diff``): ``review_builders``
- CLI preflight config writers and Click tree walk (``walk_click_tree``):
  ``cli_preflight``
- git repos, origins, worktrees (``git_in``, ``make_git_repo``):
  ``git_repos`` (plugin)
- sample client/session/state, native-daemon fake, event capture
  (``sample_state``, ``mock_native_daemon``, ``capture_events``):
  ``daemon_fakes`` (plugin)
- dispatch, repair and inbox fixtures (``tmp_dispatch_dirs``,
  ``simple_config``): ``dispatch_fixtures`` (plugin)
- every function-scope autouse fixture (``tmp_config_dir``, ``_isolate_home``,
  ``_hide_optional_binaries``, ``_raw_git_guard``): ``autouse_isolation``
  (plugin)
- session-scope autouse guards (``_git_sandbox_root``): ``session_guards``
  (plugin)
"""
