if __name__ == '__main__':
    from multiprocessing import freeze_support
    freeze_support()
    import sys
    if '--launchd-service' in sys.argv:
        sys.argv.remove('--launchd-service')
        from cachemonitor.macos_services import main
        raise SystemExit(main())
    from cachemonitor.connection_recovery import main
    raise SystemExit(main())
