if __name__ == "__main__":
    from multiprocessing import freeze_support
    freeze_support()
    import sys
    if '--cache-hook' in sys.argv:
        sys.argv.remove('--cache-hook')
        from cachemonitor.cache_control import hook_main
        raise SystemExit(hook_main())
    if '--complete-install' in sys.argv:
        sys.argv.remove('--complete-install')
        from cachemonitor.install_management import complete_main
        raise SystemExit(complete_main())
    if '--proxy-update' in sys.argv:
        sys.argv.remove('--proxy-update')
        from cachemonitor.proxy_update import main
        raise SystemExit(main())
    if '--verify-runtime' in sys.argv:
        from cachemonitor.runtime_check import main
        position = sys.argv.index('--verify-runtime') + 1
        if position >= len(sys.argv) or sys.argv[position].startswith('--'):
            if sys.stderr is not None:
                print('Usage: Codexon.exe --verify-runtime <report.json>', file=sys.stderr)
            raise SystemExit(2)
        raise SystemExit(main(sys.argv[position]))
    if '--proxy-supervisor' in sys.argv:
        sys.argv.remove('--proxy-supervisor')
        from cachemonitor.proxy_supervisor import main
        raise SystemExit(main())
    if '--model-proxy' in sys.argv:
        sys.argv.remove('--model-proxy')
        from cachemonitor.model_proxy import main
        raise SystemExit(main())
    from cachemonitor.app import main
    raise SystemExit(main())
