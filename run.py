if __name__ == "__main__":
    from multiprocessing import freeze_support
    freeze_support()
    import sys
    if '--proxy-update' in sys.argv:
        sys.argv.remove('--proxy-update')
        from cachemonitor.proxy_update import main
        raise SystemExit(main())
    if '--verify-runtime' in sys.argv:
        from cachemonitor.runtime_check import main
        raise SystemExit(main(sys.argv[sys.argv.index('--verify-runtime')+1]))
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
