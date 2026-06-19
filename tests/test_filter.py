"""
Unit tests for bagfd.filter — no network required.
"""
from bagfd.filter import FileFilter


class TestFileFilterAutoDetect:
    def test_glob_star(self):
        assert FileFilter.auto_detect('*.bundle') == 'glob'

    def test_glob_question(self):
        assert FileFilter.auto_detect('file?.txt') == 'glob'

    def test_glob_bracket(self):
        assert FileFilter.auto_detect('file[0-9].txt') == 'glob'

    def test_regex_caret(self):
        assert FileFilter.auto_detect('^ch') == 'regex'

    def test_regex_dollar(self):
        assert FileFilter.auto_detect('bundle$') == 'regex'

    def test_regex_backslash(self):
        assert FileFilter.auto_detect('\\d+') == 'regex'

    def test_regex_plus(self):
        assert FileFilter.auto_detect('ch+') == 'regex'

    def test_regex_pipe(self):
        assert FileFilter.auto_detect('a|b') == 'regex'

    def test_regex_paren(self):
        assert FileFilter.auto_detect('(abc)') == 'regex'

    def test_contains_fallback(self):
        assert FileFilter.auto_detect('ch0230') == 'contains'


class TestFileFilterMatches:
    def test_glob(self):
        f = FileFilter('*.bundle', 'glob')
        assert f.matches('foo.bundle')
        assert not f.matches('foo.txt')

    def test_glob_auto(self):
        f = FileFilter('*.bundle')
        assert f.matches('foo.bundle')

    def test_regex(self):
        f = FileFilter('^ch\\d+', 'regex')
        assert f.matches('ch0230_something')
        assert not f.matches('Image_ch0230')

    def test_regex_auto(self):
        f = FileFilter('^ch\\d+')
        assert f.matches('ch0230_foo')

    def test_contains(self):
        f = FileFilter('ch0230', 'contains')
        assert f.matches('Image_ch0230_HD')
        assert not f.matches('ch0231')

    def test_contains_auto(self):
        f = FileFilter('ch0230')
        assert f.matches('ch0230_foo.bundle')

    def test_starts_with(self):
        f = FileFilter('Image_', 'starts_with')
        assert f.matches('Image_CueSheet_001')
        assert not f.matches('ch0230_Image_')

    def test_ends_with(self):
        f = FileFilter('.bundle', 'ends_with')
        assert f.matches('foo.bundle')
        assert not f.matches('foo.bundle.bak')
