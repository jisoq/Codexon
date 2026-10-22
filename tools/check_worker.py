"""Python check bootstrap: retain stacks even when the check is not pytest."""
import faulthandler
import os
from pathlib import Path
import runpy
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.check_diagnostics import (DIAGNOSTIC_EXIT, DiagnosticFailure, diagnostic_io,
                                     emit, status, write_json)


def main():
    global stack
    folder = Path(os.environ['CODEXON_CHECK_DIAGNOSTICS'])
    # Separate files avoid races between nested runners or Python workers.
    with diagnostic_io('open worker stacks', folder / f'stacks-{os.getpid()}.log'):
        stack = (folder / f'stacks-{os.getpid()}.log').open('w', encoding='utf-8')
        faulthandler.enable(file=stack)
    deadline = time.monotonic() + float(os.environ['CODEXON_CHECK_TIMEOUT'])
    # Prefer a requested Python-thread dump while it holds the GIL. A timed
    # fallback is needed only if that thread cannot respond at the deadline.
    # Avoid asynchronous stack walks during otherwise healthy QA execution.
    def fallback():
        with diagnostic_io('schedule worker stacks', folder / f'stacks-{os.getpid()}.log'):
            faulthandler.dump_traceback_later(max(.1, deadline + 1 - time.monotonic()), file=stack)
    fallback()
    tick = threading.Event()

    def requested_stack():
        try:
            while not tick.wait(.2):
                with diagnostic_io('dump requested stacks', folder / 'stack-request'):
                    if (folder / 'stack-request').exists():
                        faulthandler.cancel_dump_traceback_later()
                        faulthandler.dump_traceback(file=stack, all_threads=True)
                        stack.flush()
                        (folder / f'stack-dumped-{os.getpid()}').touch()
                        return
        except DiagnosticFailure:
            return

    threading.Thread(target=requested_stack, daemon=True).start()
    args = sys.argv[1:]
    while args and args[0] in ('-u', '-B'):
        args.pop(0)
    if '--output' in args:
        output = Path(args[args.index('--output') + 1]).resolve()
        if not output.exists():
            from tools.check_processes import register_root
            register_root(output)
    state = folder / f'worker-state-{os.getpid()}.json'
    write_json(state, {'pid': os.getpid(), 'phase': 'executing'})
    product_exit_code = None
    try:
        if args[0] == '-m':
            sys.argv = args[1:]
            runpy.run_module(args[1], run_name='__main__', alter_sys=True)
        elif args[0] == '-c':
            sys.argv = ['-c', *args[2:]]
            exec(compile(args[1], '<string>', 'exec'), {'__name__': '__main__'})
        else:
            sys.argv = args
            sys.path.insert(0, str(Path(args[0]).resolve().parent))
            runpy.run_path(args[0], run_name='__main__')
        product_exit_code = 0
    except SystemExit as error:
        code = error.code if isinstance(error.code, int) else 0 if error.code is None else 1
        product_exit_code = None if code == DIAGNOSTIC_EXIT and status()['first_error'] else code
        raise
    except DiagnosticFailure:
        raise SystemExit(DIAGNOSTIC_EXIT)
    except BaseException:
        product_exit_code = 1
        raise
    finally:
        tick.set()
        outcome = dict(pid=os.getpid(), phase='interpreter_shutdown',
                       product_exit_code=product_exit_code, diagnostics=status())
        try:
            write_json(state, outcome)
        except DiagnosticFailure:
            pass
        try:
            fallback()
        except DiagnosticFailure:
            pass
        if status()['first_error']:
            emit(dict(product_exit_code=product_exit_code, diagnostics=status()))
            if product_exit_code == 0:
                raise SystemExit(DIAGNOSTIC_EXIT)


if __name__ == '__main__':
    try:
        main()
    except DiagnosticFailure:
        emit(dict(product_exit_code=None, diagnostics=status()))
        raise SystemExit(DIAGNOSTIC_EXIT)
