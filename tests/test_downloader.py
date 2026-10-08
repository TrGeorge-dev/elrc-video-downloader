import pytest
from playwright.sync_api import Error as PlaywrightError, sync_playwright

from elrc_downloader import normalize_url, same_lesson, safe_name, EXTRACT

BASE = 'https://elrc.shanghaitech.edu.cn/learn/videoreview/fixture-course-001'
URL = BASE + '?id=&week=2&realSection=1-2&weekDay=%E5%91%A8%E4%B8%89'

def test_original_pasted_markdown_and_escaped_ampersands():
    assert normalize_url(f'[{URL}]({URL.replace("&", chr(92) + "&")})') == URL
    assert normalize_url('  <' + URL + '>  ') == URL

@pytest.mark.parametrize('url', [URL.replace('https:', 'http:'), URL.replace('elrc.', 'fake.'),
    URL.replace('elrc.', 'user:pass@elrc.'), 'https://elrc.shanghaitech.edu.cn/learn/',
    URL.replace('.cn/', '.cn:8443/')])
def test_unrelated_urls_rejected(url):
    with pytest.raises(ValueError):
        normalize_url(url)

def test_login_redirect_must_preserve_lesson_parameters():
    assert same_lesson(URL, URL)
    assert not same_lesson(URL.replace('week=2', 'week=3'), URL)
    assert not same_lesson(BASE + '?id=', URL)
    assert not same_lesson(URL.replace('1-2', '3-4'), URL)

def test_windows_safe_names():
    assert safe_name('CON') == '_CON'
    assert safe_name('示例课程: 1/2?') == '示例课程_ 1_2_'
    assert not safe_name(' . ').endswith('.')

def test_camera_extraction_uses_player_labels():
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel='chrome', headless=True, chromium_sandbox=True)
        except PlaywrightError as error:
            message = str(error).lower()
            if 'chrome' in message and ('not found' in message or "doesn't exist" in message):
                pytest.skip('Google Chrome is not installed; DOM extraction test skipped.')
            raise
        try:
            page = browser.new_page()
            # Video attributes are synthetic. No resource is fetched from any site.
            page.route('**/*', lambda route: route.abort())
            page.set_content('''<div class="player_item">教师近景画面<div><video src="https://example.invalid/near.mp4"></video></div></div>
<div class="player_item">屏幕画面<div><video src="https://example.invalid/screen.mp4"></video></div></div>
<video></video>''')
            found = page.evaluate(EXTRACT)
            assert [(x['label'], x['url']) for x in found] == [
                ('教师近景画面', 'https://example.invalid/near.mp4'),
                ('屏幕画面', 'https://example.invalid/screen.mp4')]
        finally:
            browser.close()
