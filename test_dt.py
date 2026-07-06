try:
    from deep_translator import GoogleTranslator
    t = GoogleTranslator(source='auto', target='hi')
    res1 = t.translate('Hello')
    
    t2 = GoogleTranslator(source='auto', target='Hindi')
    res2 = t2.translate('Hello')
    
    with open('test_dt.txt', 'w', encoding='utf-8') as f:
        f.write('hi: ' + res1 + '\nHindi: ' + res2)
except Exception as e:
    with open('test_dt.txt', 'w', encoding='utf-8') as f:
        f.write('Error: ' + str(e))
