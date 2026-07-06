try:
    from deep_translator import GoogleTranslator
    t2 = GoogleTranslator(source='auto', target='hindi')
    res2 = t2.translate('Hello')
    with open('test_dt2.txt', 'w', encoding='utf-8') as f:
        f.write('hindi: ' + res2)
except Exception as e:
    with open('test_dt2.txt', 'w', encoding='utf-8') as f:
        f.write('Error: ' + str(e))
