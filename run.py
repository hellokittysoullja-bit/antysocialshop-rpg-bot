import sys
import traceback
import time

# Сразу включаем небуферизированный вывод
sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, "reconfigure") else None
sys.stderr.reconfigure(line_buffering=True) if hasattr(sys.stderr, "reconfigure") else None

print("=== Запуск run.py ===", flush=True)

try:
    import main
except Exception as e:
    print("❌ Ошибка импорта main:", file=sys.stderr, flush=True)
    traceback.print_exc()
    sys.exit(1)

print("✅ Импорт main успешен, запускаем main()...", flush=True)

try:
    main.main()
except Exception as e:
    print("❌ Ошибка в main():", file=sys.stderr, flush=True)
    traceback.print_exc()
    sys.exit(1)
