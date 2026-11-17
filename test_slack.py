"""Tests of the Slack message. Run: python3 -m unittest test_slack"""
import json
import os
import shutil
import subprocess
import unittest

PROGRAM = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'slack', 'message.jq')


def message(**values):
    arguments = {'kind': 'released', 'headline': 'Enterprise 19.0 is released', 'version': '', 'check': '', 'details': '',
                 'next': 'Approve it.', 'run': 'https://github.com/run', 'updates': '', **values}
    command = ['jq', '-n', '-f', PROGRAM] + [part for name, value in arguments.items() for part in ('--arg', name, value)]
    return json.loads(subprocess.run(command, check=True, capture_output=True, text=True).stdout)


@unittest.skipUnless(shutil.which('jq'), 'needs jq')
class MessageTest(unittest.TestCase):
    def test_a_release_has_the_version_the_check_and_a_coloured_button_to_the_updates_page(self):
        result = message(version='19.0/20261112', check='739 of 792 modules install, 52 not checked.\n', updates='https://console/admin/updates')
        self.assertEqual(result['text'], '✅ Enterprise 19.0 is released')
        self.assertEqual(result['blocks'][1]['text']['text'], 'Version: `19.0/20261112`\nInstall check: 739 of 792 modules install, 52 not checked')
        buttons = result['blocks'][-1]['elements']
        self.assertEqual([(button['text']['text'], button.get('style')) for button in buttons], [('Updates page', 'primary'), ('Open run', None)])

    def test_a_stopped_release_lists_the_failed_modules_and_escapes_what_slack_reads_as_markup(self):
        check = '669 of 687 modules install, 1 fail: broken, 17 not checked.\n`broken`: ParseError: <field name="x"> & more\n'
        result = message(kind='stopped', headline='Enterprise 18.0 was not released', check=check)
        self.assertEqual(result['text'], '⛔ Enterprise 18.0 was not released')
        self.assertEqual(result['blocks'][1]['text']['text'],
                         'Install check: 669 of 687 modules install, 1 failed, 17 not checked\n• `broken`: ParseError: &lt;field name="x"&gt; &amp; more')
        self.assertEqual([button['text']['text'] for button in result['blocks'][-1]['elements']], ['Open run'])

    def test_a_failure_without_facts_has_no_empty_block(self):
        result = message(kind='failed', headline='The check for new Odoo images failed')
        self.assertEqual([block['type'] for block in result['blocks']], ['header', 'section', 'actions'])


if __name__ == '__main__':
    unittest.main()
