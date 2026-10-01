if __name__ == "__main__":
    from multiprocessing import freeze_support
    freeze_support()
    import sys
    if '--benchmark-dashboard' in sys.argv:
        sys.argv.remove('--benchmark-dashboard')
        from cachemonitor.performance_probe import main
        try:code=main()
        except Exception:
            import json,traceback
            from pathlib import Path
            report=Path(sys.argv[sys.argv.index('--output')+1])
            report.parent.mkdir(parents=True,exist_ok=True)
            report.write_text(json.dumps({'error':traceback.format_exc()},indent=2),encoding='utf-8')
            code=1
        raise SystemExit(code)
    if '--usage-collector' in sys.argv:
        sys.argv.remove('--usage-collector')
        from cachemonitor.usage_collection import main
        raise SystemExit(main())
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
