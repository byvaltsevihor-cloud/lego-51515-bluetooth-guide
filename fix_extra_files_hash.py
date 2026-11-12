# Виправлення для SPIKE Legacy: скорочує файл /.extra_files_hash на хабі.
#
# Хаб передає по Bluetooth не більше 1023 байтів однієї відповіді. Відповідь на get_hub_info
# містить вміст цього файла, і з 86 кодами вона разом із завершальним \r має 1024 байти.
# Останній байт губиться, застосунок не може прочитати відповідь і вимагає оновлення.
# SPIKE Legacy 2.0.10 жодного з цих кодів не вимагає і сам файл не переписує.
# Звукові файли лишаються на хабі.
#
# Пояснення і порядок дій: SPIKE-LEGACY-FIX.md.
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
