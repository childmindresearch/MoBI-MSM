#!/usr/bin/env python3
"""One-shot check that the LSL marker path works before a data-collection day.

The failure this exists to catch is silent: without pylsl or liblsl the task
still runs, still writes every marker to JSONL, and shows *nothing* in
LabRecorder. The warning is one line at server startup, and it is easy to miss
with a participant already in the chair.

Two modes, chosen automatically:

  Server not running -- runs a self-test through the app's own MarkerStream
  under a clearly-marked self-test stream name, resolves it back, and checks a
  marker survives the round trip intact. This proves pylsl, liblsl, outlet
  creation, network discovery, string samples, and the JSONL mirror all work,
  without advertising a stream LabRecorder could mistake for the real one.

  Server running -- reports the real outlet exactly as LabRecorder will show
  it, and with --watch prints markers as they arrive, so an RA can click
  through the task and see the record filling up.

    .venv/bin/python scripts/check_lsl.py
    .venv/bin/python scripts/check_lsl.py --watch 120
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402

# Captured before the self-test mutates config, so the summary at the end can
# still name the stream the server will actually advertise.
REAL_NAME = config.LSL_STREAM_NAME
SELFTEST_NAME = f"{REAL_NAME}-selftest"
SELFTEST_SOURCE = f"{config.LSL_SOURCE_ID}-selftest"


def _fmt_name(fmt: int) -> str:
    """LSL's numeric channel format as the word LabRecorder shows.

    Built from the cf_* constants rather than pylsl.fmt2string, which existed in
    1.16 and is gone in 1.18.
    """
    try:
        import pylsl

        for name in dir(pylsl):
            if name.startswith("cf_") and getattr(pylsl, name) == fmt:
                return name[3:]
    except Exception:
        pass
    return str(fmt)


def _sweep(timeout: float):
    """Every stream visible on the network -- what LabRecorder lists after Update.

    resolve_streams rather than resolve_byprop: the latter returns the moment it
    has one match, which would make a second outlet advertising the same name
    invisible. That duplicate is exactly what this check needs to catch, so the
    sweep always waits out the full window and returns everything.
    """
    import pylsl

    return list(pylsl.resolve_streams(wait_time=timeout))


def _named(streams, name: str):
    return [s for s in streams if s.name() == name]


def _describe(info) -> None:
    """Print a resolved stream the way LabRecorder's list presents it."""
    print(f"    name          {info.name()}")
    print(f"    type          {info.type()}")
    print(f"    channels      {info.channel_count()}")
    print(f"    format        {_fmt_name(info.channel_format())}")
    print(f"    sample rate   {info.nominal_srate():g}"
          f"{' (irregular)' if info.nominal_srate() == 0 else ''}")
    print(f"    source_id     {info.source_id()}")
    print(f"    hostname      {info.hostname()}")
    print(f"    uid           {info.uid()}")


def _check_library() -> int | None:
    """Report the LSL install. Returns an exit code if it is unusable."""
    from app import markers

    if not markers.lsl_available():
        print("pylsl is NOT importable, so the task will run in JSONL-only mode and "
              "nothing will appear in LabRecorder.\n", file=sys.stderr)
        print("  uv pip install -e \".[lsl]\"\n", file=sys.stderr)
        print("If that installs pylsl but the import still fails, the native library is "
              "missing rather than the Python package:\n", file=sys.stderr)
        print("  brew install labstreaminglayer/tap/lsl\n", file=sys.stderr)
        try:
            import pylsl  # noqa: F401
        except Exception as exc:
            print(f"import error was: {exc}", file=sys.stderr)
        return 2

    import pylsl

    version = getattr(pylsl, "__version__", "?")
    try:
        lib = pylsl.library_info()
    except Exception:
        lib = str(pylsl.library_version())
    print(f"pylsl {version}  |  liblsl {lib}")
    return None


def _watch(info, seconds: float) -> int:
    """Subscribe to a live outlet and print markers as they arrive."""
    import pylsl

    inlet = pylsl.StreamInlet(info, max_buflen=360)
    inlet.open_stream(timeout=5.0)  # connect now, so nothing is missed before the first pull
    offset = inlet.time_correction(timeout=5.0)
    print(f"\nWatching for {seconds:g}s. Clock offset to this process: {offset * 1000:+.3f} ms.")
    print("Click through the task and markers will appear here. Ctrl-C to stop.\n")

    deadline = pylsl.local_clock() + seconds
    seen = 0
    try:
        while pylsl.local_clock() < deadline:
            sample, ts = inlet.pull_sample(timeout=1.0)
            if sample is None:
                continue
            seen += 1
            try:
                rec = json.loads(sample[0])
            except json.JSONDecodeError:
                print(f"  [{ts:.3f}] <unparseable payload> {sample[0][:120]}")
                continue
            block = rec.get("block_index")
            where = f"block {block}" if block is not None else "-"
            print(f"  [{ts:.3f}] {rec.get('event'):<28} {where:<10} "
                  f"{rec.get('participant_id') or ''}")
    except KeyboardInterrupt:
        print("\n  (stopped)")

    print(f"\n{seen} marker(s) received.")
    if seen == 0:
        print("No markers arrived. The outlet is there, but nothing happened in the task "
              "during the watch window -- open the UI and advance a step, then re-run.")
    return 0


def _selftest(resolve_timeout: float) -> int:
    """Round-trip a marker through the app's real MarkerStream code."""
    import pylsl

    from app.markers import MarkerStream

    # The real code path reads these at outlet-creation time. Overriding them
    # means this test exercises app/markers.py itself rather than a
    # reimplementation of it, while advertising a name nobody will confuse with
    # the task's stream if LabRecorder happens to be open.
    config.LSL_STREAM_NAME = SELFTEST_NAME
    config.LSL_SOURCE_ID = SELFTEST_SOURCE

    with tempfile.TemporaryDirectory(prefix="mobi-lsl-check-") as tmp:
        stream = MarkerStream(log_dir=Path(tmp))
        if stream._outlet is None:
            print("\nMarkerStream could not open an outlet even though pylsl imported. "
                  "See the traceback logged above.", file=sys.stderr)
            return 2

        found = _named(_sweep(resolve_timeout), SELFTEST_NAME)
        if not found:
            print(f"\nOpened a self-test outlet but could not resolve it back within "
                  f"{resolve_timeout:g}s.\nThe library works, but LSL discovery on this host "
                  f"does not -- check that the loopback interface is up and that a firewall "
                  f"is not blocking UDP multicast.", file=sys.stderr)
            return 1

        print(f"\nSelf-test outlet resolved ({len(found)} match):")
        _describe(found[0])

        inlet = pylsl.StreamInlet(found[0], max_buflen=10)
        # Without this the data connection is not up yet and the marker pushed
        # below goes nowhere -- an inlet only receives what is sent after it
        # connects, and connection is lazy until the first pull.
        inlet.open_stream(timeout=5.0)
        full = inlet.info(timeout=5.0)
        payload_format = full.desc().child_value("payload_format")
        print(f"    desc          payload_format={payload_format or '(missing)'}, "
              f"app={full.desc().child_value('app') or '(missing)'}")

        # An inlet needs to exist before the push, or the sample has nowhere to land.
        record = stream.push(
            "lsl_selftest",
            session_id="selftest",
            participant_id="SELFTEST",
            block_index=0,
            note="round-trip check from scripts/check_lsl.py",
        )
        sample, ts = inlet.pull_sample(timeout=5.0)
        if sample is None:
            print("\nPushed a marker but it never arrived at the inlet.", file=sys.stderr)
            return 1

        received = json.loads(sample[0])
        ok = received == record
        drift_us = (ts - record["t_lsl"]) * 1e6

        print("\nRound trip:")
        print(f"    payload       {'identical' if ok else 'MISMATCH'}")
        print(f"    event         {received.get('event')}")
        print(f"    t_lsl (sent)  {record['t_lsl']:.6f}")
        print(f"    timestamp     {ts:.6f}  (delta {drift_us:+.3f} us)")

        mirrored = (Path(tmp) / "selftest.jsonl").read_text().strip().splitlines()
        print(f"    JSONL mirror  {len(mirrored)} line(s) written")

        if not ok:
            print("\nThe payload changed in transit, which should be impossible for a "
                  "string channel. Treat this install as broken.", file=sys.stderr)
            return 1
        if abs(drift_us) > 1.0:
            print(f"\nNote: the recorded timestamp differs from the t_lsl in the JSONL by "
                  f"{drift_us:+.3f} us. They are meant to be the same number -- see the "
                  f"push_sample call in app/markers.py.")

    print("\nLSL is working. When you start the server, LabRecorder will show:\n")
    print(f"    {REAL_NAME}  (type {config.LSL_STREAM_TYPE}, 1 string channel, irregular rate)")
    print("\n    .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8420")
    print("\nThen hit Update in LabRecorder. The self-test stream above is already gone.")
    print("\n(liblsl may log \"Stream transmission broke off\" below as this process exits. "
          "That is\nthe self-test outlet being torn down, not a failure.)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--watch", type=float, default=0, metavar="SECONDS",
                    help="If the task's outlet is live, subscribe and print markers as they "
                         "arrive for this many seconds.")
    ap.add_argument("--timeout", type=float, default=5.0,
                    help="Seconds to wait for stream resolution (default 5).")
    args = ap.parse_args()

    failed = _check_library()
    if failed is not None:
        return failed

    streams = _sweep(args.timeout)
    others = [s for s in streams if s.name() != REAL_NAME]
    if others:
        print(f"\nAlso on the network ({len(others)} other stream(s) LabRecorder will list):")
        for info in others:
            print(f"    {info.name()}  (type {info.type()}, {info.channel_count()} ch, "
                  f"{info.nominal_srate():g} Hz, host {info.hostname()})")

    live = _named(streams, REAL_NAME)
    if live:
        print(f"\nThe task's marker outlet is LIVE -- this is what LabRecorder shows "
              f"after Update:")
        for info in live:
            _describe(info)
        if len(live) > 1:
            print(f"\nWARNING: {len(live)} outlets are advertising {config.LSL_STREAM_NAME}. "
                  f"That usually means two copies of the server are running, and a recording "
                  f"would interleave markers from both. Stop one before collecting.")
            return 1
        if args.watch:
            return _watch(live[0], args.watch)
        print("\nNothing else to check -- hit Record in LabRecorder when the participant is "
              "ready.\nRe-run with --watch 120 to see markers arrive as you click through.")
        return 0

    print(f"\nNo outlet named {REAL_NAME} on the network, so the server is not "
          f"running.\nChecking the library end to end instead.")
    return _selftest(args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
