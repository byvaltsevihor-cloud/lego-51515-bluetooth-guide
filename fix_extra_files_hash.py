# Виправлення для SPIKE Legacy: скорочує службовий список .extra_files_hash на хабі.
#
# Хаб не надсилає символ кінця повідомлення у відповіді на get_hub_info, довшій за 1022 символи.
# Застосунок не може прочитати таку відповідь і вимагає оновлення. Для SPIKE Legacy 2.0.10 ці
# записи не потрібні: у коді застосунку список додаткових файлів не заданий. Самі звукові файли
# лишаються на хабі.
#
# Перед запуском збережіть оригінал і перевірте потребу. Див. розділ 1 у TROUBLESHOOTING.md.
# Запуск: ./hub run fix_extra_files_hash.py

PATH = '.extra_files_hash'
KEEP = 30

parts = open(PATH).read().strip().split(';')
if len(parts) <= KEEP:
    print('Нічого не змінено: у списку лише %d записів.' % len(parts))
else:
    with open(PATH, 'w') as output:
        output.write(';'.join(parts[:KEEP]))
    print('Було записів: %d, стало: %d' % (len(parts), len(open(PATH).read().split(';'))))
