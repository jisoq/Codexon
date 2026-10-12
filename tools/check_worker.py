"""Python check bootstrap: retain stacks even when the check is not pytest."""
import faulthandler
import json
import os
from pathlib import Path
import runpy
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    global stack
    folder = Path(os.environ['CODEXON_CHECK_DIAGNOSTICS'])
    # Separate files avoid races between nested runners or Python workers.
    stack = (folder / f'stacks-{os.getpid()}.log').open('w', encoding='utf-8')
    faulthandler.enable(file=stack)
    faulthandler.dump_traceback_later(60, repeat=True, file=stack)
    tick = threading.Event()

    def requested_stack():
        while not tick.wait(.2):
            if (folder / 'stack-request').exists():
                faulthandler.dump_traceback(file=stack, all_threads=True)
                stack.flush()
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
    state.write_text(json.dumps({'pid': os.getpid(), 'phase': 'executing'}))
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
    finally:
        state.write_text(json.dumps({'pid': os.getpid(), 'phase': 'interpreter_shutdown'}))
        # Keep the watcher and descriptor alive through interpreter shutdown.
        # pytest owns/cancels the global timer during its own session.
        faulthandler.dump_traceback_later(60, repeat=True, file=stack)


if __name__ == '__main__':
    main()
