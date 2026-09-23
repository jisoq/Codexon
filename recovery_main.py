if __name__ == '__main__':
    from multiprocessing import freeze_support
    freeze_support()
    from cachemonitor.connection_recovery import main
    raise SystemExit(main())
