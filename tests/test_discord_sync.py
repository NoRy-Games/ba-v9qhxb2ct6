"""Regression tests for rendering and publishing, using no real credentials or network."""
import copy
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.discord_sync.render import render
from scripts.discord_sync import sync
from scripts.discord_sync.assets import resolve_assets


GUILD = '1565754528823775396'
BOT = '1569487974910136400'
BOT_ROLE = '1569496972363038865'
CHANNELS = {'changelog': '1569512033232093264', 'characters': '1569512097002295408',
            'balance': '1569512251399082124'}
VIEW, SEND, HISTORY, EMBED, PIN = 1 << 10, 1 << 11, 1 << 16, 1 << 14, 1 << 51
YELLOW = '\x1b[1;33m'
ANSI = re.compile(r'\x1b\[[0-9;]*m')


def visible_text(payload):
    parts = [payload.get('content', '')]
    for embed in payload.get('embeds', []):
        parts.extend(embed.get(name, '') for name in ('title', 'description', 'url'))
        parts.append(embed.get('footer', {}).get('text', ''))
        parts.extend(embed.get('author', {}).get(name, '') for name in ('name', 'url'))
        for field in embed.get('fields', []):
            parts.extend((field['name'], field['value']))
    return '\n'.join(parts)


def field_for(row, tag):
    return next(field['value'] for embed in row['payload'].get('embeds', [])
                for field in embed.get('fields', []) if '`' + tag + '`' in field['value'])


def numeric_row(field, label):
    lines = ANSI.sub('', field).splitlines()
    return next(line for line in lines if re.match(r'^\s*\*?\s*' + re.escape(label) + r'\b', line, re.I))


def highlight_count(rows):
    return sum(field['value'].count(YELLOW) for row in rows if row['channel'] == 'balance'
               for embed in row['payload'].get('embeds', []) for field in embed.get('fields', []))


class RepositoryCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='discord-sync-test-')
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / 'repo'
        self.repo.mkdir()
        shutil.copy2(ROOT / 'balance.json', self.repo / 'balance.json')
        shutil.copytree(ROOT / 'wiki', self.repo / 'wiki')
        shutil.copytree(ROOT / 'changelog', self.repo / 'changelog')
        self.state_path = Path(self.temp.name) / 'state.json'
        self.config = {'guild_id': GUILD, 'bot_id': BOT, 'bot_role_id': BOT_ROLE,
                       'channel_ids': CHANNELS.copy(), 'seed_bindings': {},
                       'repository': 'NoRy-Games/ba-v9qhxb2ct6', 'source_language': 'en',
                       'asset_ref': '97bd3babc457798bfc39604f11a30a82b5f3cd23',
                       'state_path': str(self.state_path)}

    def read_json(self, relative):
        return json.loads((self.repo / relative).read_text())

    def write_json(self, relative, value):
        (self.repo / relative).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')

    def rendered(self, bindings=None):
        return render(self.repo, self.config, bindings or {})

    def set_regression_values(self):
        """Freeze the 15 known edits inside this test, without freezing future repo data."""
        original = self.read_json('wiki/original-values.json')
        current = self.read_json('balance.json')
        original['skills'] = copy.deepcopy(current['skills'])
        before = {skill['tag']: skill for skill in original['skills']}
        after = {skill['tag']: skill for skill in current['skills']}
        changes = [
            ('Skill.JTR.OraRush', 'damage', 40, 34),
            ('Skill.JTR.TheWorldSTP', 'duration', 1.5, 2),
            ('Skill.JTR.TheWorldSTP', 'statusDuration', 1.5, 2),
            ('Skill.DIO.MudaRush', 'damage', 38, 32),
            ('Skill.DIO.TheWorldWLD', 'duration', 1.5, 3),
            ('Skill.DIO.TheWorldWLD', 'statusDuration', 1.5, 3),
            ('Skill.JSK.InstantWall', 'duration', 20, 5),
            ('Skill.JSK.InstantWall', 'statusDuration', 20, 5),
            ('Skill.DOH.WRYRush', 'damage', 45, 38),
            ('Skill.DOH.RoadRoller', 'damage', 120, 102),
            ('Skill.DVL.CrimsonDimension', 'duration', 3.5, 6.5),
            ('Skill.DVL.CrimsonDimension', 'statusDuration', 3.5, 6.5),
            ('Skill.JR6.TheWorldSP6', 'duration', 1.75, 2.25),
            ('Skill.JR6.TheWorldSP6', 'statusDuration', 1.75, 2.25),
            ('Skill.ABC.MoodyRush', 'damage', 29, 25),
        ]
        for tag, field, old, new in changes:
            before[tag][field], after[tag][field] = old, new
        # Equivalent floating representations must remain unmarked and readable.
        before['Skill.C28.RobMd']['range'] = 8.700000000000001
        after['Skill.C28.RobMd']['range'] = 8.7
        self.write_json('wiki/original-values.json', original)
        self.write_json('balance.json', current)


class RendererTests(RepositoryCase):
    def test_current_reference_uses_corrected_original_time_stops(self):
        self.set_regression_values()
        rows = {row['key']: row for row in self.rendered()}
        for key, tag, original, current in (
                ('balance:JTR', 'Skill.JTR.TheWorldSTP', '1.5', '2'),
                ('balance:DIO', 'Skill.DIO.TheWorldWLD', '1.5', '3'),
                ('balance:JR6', 'Skill.JR6.TheWorldSP6', '1.75', '2.25')):
            with self.subTest(character=key):
                field = field_for(rows[key], tag)
                for label in ('Duration', 'Effect'):
                    line = numeric_row(field, label)
                    self.assertRegex(line, r'\s' + re.escape(original) + r'\s+' + re.escape(current) + r'(?:\.0)?\s*$')
                    self.assertIn(YELLOW + '*', field)
        all_rows = list(rows.values())
        self.assertEqual(highlight_count(all_rows), 15)
        text = '\n'.join(visible_text(row['payload']) for row in all_rows if row['channel'] == 'balance')
        self.assertNotRegex(text, r'\d+\.\d{10,}')
        heroes = {skill['character'] for skill in self.read_json('balance.json')['skills']}
        self.assertEqual(len([row for row in all_rows if row['channel'] == 'balance']), len(heroes) + 1)

    def test_future_version_and_original_changes_update_counts(self):
        self.set_regression_values()
        original = self.read_json('wiki/original-values.json')
        current = self.read_json('balance.json')
        by_tag = {skill['tag']: skill for skill in current['skills']}
        for skill in original['skills']:
            if skill['tag'] == 'Skill.JTR.TheWorldSTP':
                for field in ('duration', 'statusDuration'):
                    skill[field] = by_tag[skill['tag']][field]
            if skill['tag'] == 'Skill.JTR.OraRush':
                skill['damage'] = by_tag[skill['tag']]['damage']
        current.update(version='v40', changelog='changelog/v40.md')
        self.write_json('wiki/original-values.json', original)
        self.write_json('balance.json', current)
        (self.repo / 'changelog/v40.md').write_text('# v40\n## Balance\n- Updated original reference values.\n')
        rows = self.rendered()
        self.assertEqual(highlight_count(rows), 12)
        header = next(row for row in rows if row['key'] == 'balance:index')
        text = visible_text(header['payload'])
        self.assertIn('v40', text)
        self.assertRegex(text, r'6 heroes')
        self.assertRegex(text, r'8 skills')
        self.assertRegex(text, r'12 (?:changed )?fields')

    def test_wiki_descriptions_refresh_and_visible_text_has_no_github_links(self):
        path = self.repo / 'wiki/characters/00-jotaro-kujo.md'
        marker = 'A focused rush that pushes nearby opponents away.'
        source = path.read_text()
        skill_rows = [line for line in source.splitlines() if '| `Skill.' in line]
        self.assertTrue(skill_rows)
        description = skill_rows[0].strip('|').split('|')[-1].strip()
        path.write_text(source.replace(description, marker))
        rows = self.rendered()
        hero = next(row for row in rows if row['key'] == 'character:JTR')
        self.assertIn(marker, visible_text(hero['payload']))
        for row in rows:
            self.assertNotRegex(visible_text(row['payload']), r'https?://\S*(?:github\.com|githubusercontent\.com)')
        ultimate_description = skill_rows[2].strip('|').split('|')[-1].strip()
        self.assertIn(ultimate_description, visible_text(hero['payload']))

    def test_public_guide_keeps_mechanics_qualitative_and_admin_values_exact(self):
        self.set_regression_values()
        descriptions = {
            'Skill.JSP.Overdrive': 'Boosts your damage and speed, and you take only 70% damage.',
            'Skill.DVL.CrimsonDimension': 'You cannot be hit. Everyone else in range moves at half speed.',
            'Skill.C28.RobSd': 'A hit steals their Stand: no skills and half damage.',
            'Skill.C28.RobMd': 'Hold to aim, release. Takes half their health and seals their ultimate.',
            'Skill.HHS.EmperorTyphoon': 'Fires three bullets. Deals 40 damage. Stuns enemies for 5 seconds. Has a 30-second cooldown.',
        }
        for path in (self.repo / 'wiki/characters').glob('*.md'):
            lines = path.read_text().splitlines()
            for index, line in enumerate(lines):
                for tag, description in descriptions.items():
                    if '`' + tag + '`' in line:
                        prefix, _, _ = line.rsplit('|', 2)
                        lines[index] = prefix + '| ' + description + ' |'
            path.write_text('\n'.join(lines) + '\n')
        rows = {row['key']: row for row in self.rendered()}
        public = '\n'.join(visible_text(rows['character:' + ident]['payload'])
                           for ident in ('JSP', 'DVL', 'C28', 'HHS'))
        self.assertIn('Boosts your damage and speed, and you take less damage.', public)
        self.assertIn('Everyone else in range moves more slowly.', public)
        self.assertIn('no skills and reduced damage', public)
        self.assertIn('Drains their health and seals their ultimate', public)
        self.assertIn('Fires three bullets.', public)
        self.assertIn('Deals damage. Stuns enemies temporarily. Has a cooldown.', public)
        self.assertNotRegex(public, r'70%|half (?:speed|damage|their health)|40 damage|5 seconds|30-second')
        admin = field_for(rows['balance:JTR'], 'Skill.JTR.OraRush')
        self.assertRegex(numeric_row(admin, 'Damage'), r'40\s+34\s*$')
        self.assertEqual(highlight_count(list(rows.values())), 15)

    def test_long_changelog_splits_without_changing_existing_keys(self):
        old_rows = self.rendered()
        current = self.read_json('balance.json')
        current.update(version='v40', changelog='changelog/v40.md')
        self.write_json('balance.json', current)
        notes = [f'- Change {index}: a documented gameplay adjustment for this version.' for index in range(180)]
        (self.repo / 'changelog/v40.md').write_text('# v40\n## Updates\n' + '\n'.join(notes) + '\n')
        rows = self.rendered()
        parts = [row for row in rows if row['key'].startswith('changelog:v40')]
        self.assertGreater(len(parts), 1)
        self.assertEqual(parts[0]['key'], 'changelog:v40')
        self.assertEqual(parts[1]['key'], 'changelog:v40:2')
        self.assertTrue({row['key'] for row in old_rows}.issubset({row['key'] for row in rows}))
        text = '\n'.join(part['payload'].get('content', '') for part in parts)
        for note in notes:
            self.assertIn(note, text)
        self.assertTrue(all(len(part['payload'].get('content', '').encode('utf-16-le')) // 2 <= 2000 for part in parts))
        self.assertEqual([row['key'] for row in rows], [row['key'] for row in self.rendered({'character:JTR': '999'})])

    def test_changelog_tables_show_labeled_values_instead_of_raw_markdown(self):
        sources = {version: (self.repo / f'changelog/{version}.md').read_bytes()
                   for version in ('v33', 'v34', 'v35')}
        rows = {row['key']: row for row in self.rendered()}
        text = {version: rows['changelog:' + version]['payload']['content'] for version in sources}
        for version, content in text.items():
            self.assertNotIn('|', content)
            self.assertNotRegex(content, r'(?m)^\s*:?-{3,}:?(?:\s*\|\s*:?-{3,}:?)+\s*$')
            self.assertEqual((self.repo / f'changelog/{version}.md').read_bytes(), sources[version])
        self.assertIn('- **Stopped time with the enemy ~10 m away** — **Now:** Only with the enemy close:', text['v33'])
        self.assertIn('**Before:** 40; **Now:** **32** (−20%)', text['v33'])
        self.assertIn('**Jotaro / Star Platinum — Ora Rush** — **Original:** 40; **v33:** 32; **Now:** **34** (−15%)', text['v34'])
        self.assertIn('**Road Roller**', text['v34'].replace('DIO (High) / The World — ', ''))
        self.assertIn('**Original:** 120; **v33:** 102; **Now:** 102 (unchanged)', text['v34'])
        self.assertIn('- **Erased time** — **Before:** 3.5 s; **Now:** **6.5 s**', text['v35'])
        self.assertIn('everyone within 50 m moves at half speed', text['v35'])

    def test_changelog_table_alignment_escaped_pipes_and_literal_code(self):
        current = self.read_json('balance.json')
        current.update(version='v40', changelog='changelog/v40.md')
        self.write_json('balance.json', current)
        command = "Run `printf 'A|B' | cat` to inspect the result."
        fenced = '\n'.join(('````sh', '| Command | Result |', '|---|---|', '| A | B |',
                            '```', '| Still code | Before | Now |', '|---|---|---|',
                            '| C | 1 | 2 |', '````'))
        tildes = '\n'.join(('~~~text', '| A | B |', '|---|---|', '| C | D |', '~~~'))
        notes = '\n'.join(('# v40', '## Commands', command, '', fenced, '', tildes, '',
                           '## Balance', 'Rule | Before | Now', ':--- | :---: | ---:',
                           r'Choose A \| B | 5 \| 6 | **8**',
                           r'Path \\ | 1 | 2', '',
                           'Before | Now', '--- | ---', '4 | 7', ''))
        (self.repo / 'changelog/v40.md').write_text(notes)
        row = next(row for row in self.rendered() if row['key'] == 'changelog:v40')
        content = row['payload']['content']
        self.assertIn(command, content)
        self.assertIn(fenced, content)
        self.assertIn(tildes, content)
        self.assertIn('- **Choose A | B** — **Before:** 5 | 6; **Now:** **8**', content)
        self.assertIn(r'- **Path \\** — **Before:** 1; **Now:** 2', content)
        self.assertIn('- **Before:** 4; **Now:** 7', content)
        self.assertNotIn(':--- | :---: | ---:', content)

    def test_long_converted_changelog_table_keeps_values_and_stable_parts(self):
        original_keys = {row['key'] for row in self.rendered()}
        current = self.read_json('balance.json')
        current.update(version='v40', changelog='changelog/v40.md')
        self.write_json('balance.json', current)
        table = ['# v40', '## Balance', '| Skill | Original | Now |', '|:---|---:|:---:|']
        table += [f'| Hero {n} | {n} | **{n + 1}** |' for n in range(100)]
        (self.repo / 'changelog/v40.md').write_text('\n'.join(table) + '\n')
        rows = self.rendered()
        parts = [row for row in rows if row['key'].startswith('changelog:v40')]
        self.assertGreater(len(parts), 1)
        self.assertEqual([row['key'] for row in parts],
                         ['changelog:v40'] + [f'changelog:v40:{n}' for n in range(2, len(parts) + 1)])
        self.assertTrue(original_keys.issubset({row['key'] for row in rows}))
        text = '\n'.join(row['payload']['content'] for row in parts)
        self.assertNotIn('|', text)
        for n in range(100):
            self.assertIn(f'- **Hero {n}** — **Original:** {n}; **Now:** **{n + 1}**', text)
        self.assertTrue(all(len(row['payload']['content'].encode('utf-16-le')) // 2 <= 1900 for row in parts))
        self.assertEqual([row['key'] for row in rows], [row['key'] for row in self.rendered()])

    def test_missing_original_field_is_a_dash_instead_of_zero(self):
        original = self.read_json('wiki/original-values.json')
        next(skill for skill in original['skills'] if skill['tag'] == 'Skill.JTR.OraRush').pop('areaDamage')
        self.write_json('wiki/original-values.json', original)
        row = next(row for row in self.rendered() if row['key'] == 'balance:JTR')
        field = field_for(row, 'Skill.JTR.OraRush')
        self.assertRegex(numeric_row(field, 'Area damage'), r'—\s+0(?:\.0)?\s*$')
        self.assertRegex(numeric_row(field, 'Hits'), r'—\s+—\s*$')

    def test_declared_non_english_source_is_rejected(self):
        self.config['source_language'] = 'pt-BR'
        with self.assertRaises((ValueError, sync.SyncError)):
            self.rendered()

    def test_clearly_portuguese_notes_fail_without_translation(self):
        current = self.read_json('balance.json')
        current.update(version='v40', changelog='changelog/v40.md')
        self.write_json('balance.json', current)
        (self.repo / 'changelog/v40.md').write_text('# v40\n## Alterações\n- O personagem agora causa mais dano aos inimigos.\n')
        with self.assertRaisesRegex(ValueError, 'English'):
            self.rendered()


class AssetRevisionTests(RepositoryCase):
    def setUp(self):
        super().setUp()
        self.images = self.repo / 'wiki/images'
        self.images.mkdir(exist_ok=True)
        self.first = self.images / '00-jotaro-kujo.png'
        self.second = self.images / '01-dio.png'
        self.first.write_bytes(b'portrait fixture one, revision one')
        self.second.write_bytes(b'portrait fixture two, revision one')
        self.git('init', '--quiet')
        self.commit('Initial source and portraits')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=Discord Sync Tests',
                               '-c', 'user.email=discord-sync-tests@example.invalid',
                               '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null', *args],
                              check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, message):
        self.git('add', '--all')
        self.git('commit', '--quiet', '-m', message)
        return self.git('rev-parse', 'HEAD')

    def test_unrelated_commits_keep_immutable_portrait_urls_unchanged(self):
        image_commit = self.git('rev-parse', 'HEAD')
        initial_config = copy.deepcopy(self.config)
        before = resolve_assets(self.repo, self.config)
        (self.repo / 'README.md').write_text('An unrelated documentation change.\n')
        latest_commit = self.commit('Change prose without changing portraits')
        after = resolve_assets(self.repo, self.config)
        self.assertNotEqual(image_commit, latest_commit)
        self.assertEqual(before['asset_refs'], after['asset_refs'])
        self.assertEqual(self.config, initial_config)
        self.assertEqual(after['asset_refs']['wiki/images/00-jotaro-kujo.png'], image_commit)
        row = next(row for row in render(self.repo, after, {}) if row['key'] == 'character:JTR')
        thumbnail = row['payload']['embeds'][0]['thumbnail']['url']
        self.assertIn('/' + image_commit + '/wiki/images/00-jotaro-kujo.png', thumbnail)
        self.assertNotIn('/main/', thumbnail)

    def test_changing_one_portrait_moves_only_its_revision(self):
        first_commit = self.git('rev-parse', 'HEAD')
        self.first.write_bytes(b'portrait fixture one, revision two')
        image_commit = self.commit('Replace only the Jotaro portrait')
        resolved = resolve_assets(self.repo, self.config)
        self.assertEqual(resolved['asset_refs']['wiki/images/00-jotaro-kujo.png'], image_commit)
        self.assertEqual(resolved['asset_refs']['wiki/images/01-dio.png'], first_commit)

    def test_untracked_portrait_is_rejected_instead_of_guessing_a_revision(self):
        (self.images / 'untracked.png').write_bytes(b'not committed')
        with self.assertRaisesRegex(ValueError, 'immutable Git revision'):
            resolve_assets(self.repo, self.config)


class FakeDiscord:
    """Enough Discord behavior to exercise safety, reconciliation and crash recovery."""
    def __init__(self, rows, bindings):
        self.messages = {}
        self.pins = {cid: set() for cid in CHANNELS.values()}
        self.calls = []
        self.created = 0
        self.next_id = 1700000000000000000
        self.crash_after_create = False
        self.nonadmin_can_view_balance = False
        for row in rows:
            if row['key'] in bindings:
                ident = bindings[row['key']]
                self.messages[ident] = self.message(row['payload'], ident, CHANNELS[row['channel']])

    def message(self, payload, ident, cid):
        result = copy.deepcopy(payload)
        result.update(id=ident, channel_id=cid, author={'id': BOT, 'bot': True},
                      mentions=[], mention_roles=[], mention_everyone=False)
        for embed in result.get('embeds', []):
            embed['type'] = 'rich'
            if 'thumbnail' in embed:
                embed['thumbnail'].update(proxy_url='https://discord.invalid/proxy', width=128,
                                          height=128, content_type='image/png', flags=0,
                                          placeholder='placeholder', placeholder_version=1)
        return result

    @property
    def mutations(self):
        return [call for call in self.calls if call[0] != 'GET']

    @property
    def content_mutations(self):
        return [call for call in self.calls if call[0] in ('POST', 'PATCH') and '/messages' in call[1]]

    def request(self, method, path, payload=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        clean = path.split('?', 1)[0]
        if method == 'GET' and clean == '/users/@me':
            return {'id': BOT, 'bot': True}
        if method == 'GET' and clean == f'/guilds/{GUILD}':
            return {'id': GUILD, 'owner_id': '900000000000000001', 'roles': [
                {'id': GUILD, 'name': '@everyone', 'permissions': str(VIEW | HISTORY)},
                {'id': BOT_ROLE, 'name': 'NoRy', 'permissions': str(VIEW | SEND | HISTORY | EMBED | PIN)},
                {'id': '900000000000000003', 'name': 'Untrusted', 'permissions': '0'}]}
        if method == 'GET' and clean == f'/guilds/{GUILD}/members/{BOT}':
            return {'user': {'id': BOT, 'bot': True}, 'roles': [BOT_ROLE]}
        for channel, cid in CHANNELS.items():
            if method == 'GET' and clean == f'/channels/{cid}':
                overwrites = [{'id': GUILD, 'type': 0, 'allow': '0', 'deny': str(VIEW)}] if channel == 'balance' else []
                overwrites.append({'id': BOT_ROLE, 'type': 0, 'allow': str(VIEW | SEND | HISTORY | EMBED | PIN), 'deny': '0'})
                if channel == 'balance' and self.nonadmin_can_view_balance:
                    overwrites.append({'id': '900000000000000003', 'type': 0, 'allow': str(VIEW), 'deny': '0'})
                return {'id': cid, 'guild_id': GUILD, 'type': 0,
                        'name': {'characters': 'characters-and-skills', 'balance': 'balance-admin'}.get(channel, channel),
                        'permission_overwrites': overwrites}
            if method == 'GET' and clean == f'/channels/{cid}/messages/pins':
                return {'items': [{'message': self.messages[ident]} for ident in self.pins[cid]], 'has_more': False}
            if method == 'GET' and clean == f'/channels/{cid}/messages':
                return [copy.deepcopy(message) for message in reversed(list(self.messages.values())) if message['channel_id'] == cid]
            prefix = f'/channels/{cid}/messages/'
            if clean.startswith(prefix):
                suffix = clean[len(prefix):]
                if method == 'PUT' and suffix.startswith('pins/'):
                    ident = suffix.split('/')[-1]
                    self.pins[cid].add(ident)
                    self.messages[ident]['pinned'] = True
                    return None
                if method == 'GET':
                    return copy.deepcopy(self.messages[suffix])
                if method == 'PATCH':
                    self.messages[suffix] = self.message(payload, suffix, cid)
                    return copy.deepcopy(self.messages[suffix])
            if method == 'POST' and clean == f'/channels/{cid}/messages':
                nonce = payload.get('nonce')
                if payload.get('enforce_nonce') and nonce is not None:
                    existing = next((message for message in self.messages.values()
                                     if message['channel_id'] == cid and message.get('nonce') == nonce), None)
                    if existing is not None:
                        return copy.deepcopy(existing)
                self.next_id += 1
                ident = str(self.next_id)
                self.messages[ident] = self.message(payload, ident, cid)
                self.created += 1
                if self.crash_after_create:
                    self.crash_after_create = False
                    raise RuntimeError('Simulated lost create response')
                return copy.deepcopy(self.messages[ident])
        raise AssertionError(f'Unexpected fake request: {method} {path}')


class PublisherTests(RepositoryCase):
    def seeded(self):
        rows = self.rendered()
        bindings = {row['key']: str(1600000000000000000 + index) for index, row in enumerate(rows)}
        self.config['seed_bindings'] = bindings.copy()
        rows = self.rendered(bindings)
        return FakeDiscord(rows, bindings), bindings

    def engine(self, client, render_func=render):
        state = sync.load_state(self.state_path, self.config)
        return sync.SyncEngine(self.config, state, client, self.repo, render_func, self.state_path)

    def test_untrusted_private_channel_allowance_fails_before_writes(self):
        client, _ = self.seeded()
        client.nonadmin_can_view_balance = True
        with self.assertRaises(sync.SyncError):
            self.engine(client).run(apply=True)
        self.assertEqual(client.mutations, [])

    def test_wrong_message_author_fails_before_any_write(self):
        client, bindings = self.seeded()
        client.messages[bindings['balance:JTR']]['author']['id'] = '900000000000000009'
        with self.assertRaises(sync.SyncError):
            self.engine(client).run(apply=True)
        self.assertEqual(client.mutations, [])

    def test_dry_run_is_read_only_and_does_not_create_state(self):
        client, _ = self.seeded()
        self.engine(client).run(apply=False)
        self.assertEqual(client.mutations, [])
        self.assertFalse(self.state_path.exists())

    def test_apply_preserves_seed_ids_indexes_and_is_idempotent(self):
        client, bindings = self.seeded()
        client.messages[bindings['balance:JTR']]['embeds'][0]['description'] = 'Stale reference.'
        self.engine(client).run(apply=True)
        self.assertTrue(any(call[0] == 'PATCH' for call in client.content_mutations))
        self.assertEqual(client.created, 0)
        state = sync.load_state(self.state_path, self.config)
        self.assertEqual(state['bindings'], bindings)
        for channel in CHANNELS:
            header = client.messages[bindings[channel + ':index']]
            self.assertIn(bindings[channel + ':index'], client.pins[CHANNELS[channel]])
            if channel == 'characters':
                self.assertIn(bindings['character:JTR'], visible_text(header))
        client.calls.clear()
        self.engine(client).run(apply=True)
        self.assertEqual(client.content_mutations, [])

    def test_state_write_failure_leaves_previous_json_intact(self):
        previous = {'version': 1, 'guild_id': GUILD, 'bot_id': BOT, 'bindings': {}, 'pending': {}}
        sync.write_state_atomic(self.state_path, previous)
        old_bytes = self.state_path.read_bytes()
        with mock.patch('os.replace', side_effect=OSError('Simulated disk failure')):
            with self.assertRaises(sync.SyncError):
                sync.write_state_atomic(self.state_path, dict(previous, bindings={'changelog:index': '123'}))
        self.assertEqual(self.state_path.read_bytes(), old_bytes)
        self.assertEqual(json.loads(self.state_path.read_text()), previous)

    def test_lost_create_response_recovers_without_duplicate_message(self):
        client, _ = self.seeded()

        def with_new_entry(repo, config, bindings):
            rows = render(repo, config, bindings)
            rows.append({'key': 'changelog:v40', 'channel': 'changelog', 'payload': {'content': '# v40\n- A new documented release.'}})
            return rows

        client.crash_after_create = True
        with self.assertRaises((RuntimeError, sync.SyncError)):
            self.engine(client, with_new_entry).run(apply=True)
        self.assertEqual(client.created, 1)
        pending_state = sync.load_state(self.state_path, self.config)
        self.assertIn('changelog:v40', pending_state['pending'])
        self.engine(client, with_new_entry).run(apply=True)
        self.assertEqual(client.created, 1)
        final_state = sync.load_state(self.state_path, self.config)
        self.assertIn('changelog:v40', final_state['bindings'])
        self.assertNotIn('changelog:v40', final_state['pending'])


class TransportTests(unittest.TestCase):
    def test_rate_limit_retry_honors_retry_after_without_real_sleep(self):
        client = sync.DiscordClient('dummy-discord-token-for-unit-tests')
        limited = urllib.error.HTTPError('https://discord.com/api/v10/users/@me', 429,
                                         'rate limited', {}, io.BytesIO(b'{"retry_after":0.25}'))
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"id":"123"}'
        with mock.patch.object(client.opener, 'open', side_effect=[limited, response]) as opened:
            with mock.patch.object(sync.time, 'sleep') as sleep:
                self.assertEqual(client.request('GET', '/users/@me'), {'id': '123'})
        self.assertEqual(opened.call_count, 2)
        sleep.assert_called_once_with(0.25)

    def test_error_does_not_echo_token_or_discord_response_body(self):
        token = 'dummy-discord-token-for-unit-tests'
        client = sync.DiscordClient(token)
        body = json.dumps({'message': 'response body includes ' + token, 'code': 50001}).encode()
        failed = urllib.error.HTTPError('https://discord.com/api/v10/users/@me', 401,
                                        'failed', {}, io.BytesIO(body))
        with mock.patch.object(client.opener, 'open', side_effect=failed):
            with self.assertRaises(sync.ApiError) as raised:
                client.request('GET', '/users/@me')
        self.assertNotIn(token, str(raised.exception))
        self.assertNotIn('response body includes', str(raised.exception))

    def test_redirects_and_mutating_access_operations_are_refused(self):
        self.assertIsNone(sync.NoRedirects().redirect_request(None, None, 302, '', {}, 'https://other.invalid/'))
        client = sync.DiscordClient('dummy-discord-token-for-unit-tests')
        with mock.patch.object(client.opener, 'open') as opened:
            for method, path in [('DELETE', '/channels/123/messages/456'),
                                 ('PUT', '/channels/123/permissions/456'),
                                 ('GET', 'https://other.invalid/users/@me')]:
                with self.subTest(method=method, path=path):
                    with self.assertRaises(sync.SyncError):
                        client.request(method, path)
            opened.assert_not_called()


if __name__ == '__main__':
    unittest.main()
