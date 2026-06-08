"""Unit tests for claude_config.py - pure mutators and masking, no file I/O.

All token values here are made up; never put a real credential in a test.
"""

import os
import shutil
import tempfile
import unittest

import claude_config as cc

FAKE_PAT = "ghp_FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE0000"

SAMPLE_SSH_CONFIG = """\
Host github.com
    HostName github.com
    User git
    IdentityFile {keydir}/github
    IdentitiesOnly yes

Host ovh
    HostName 1.2.3.4
    User ubuntu
    IdentityFile {keydir}/ovh_ed25519

Host *
    AddKeysToAgent yes
"""


class BypassTests(unittest.TestCase):
    def test_on(self):
        s = cc.set_bypass({}, True)
        self.assertEqual(s["permissions"]["defaultMode"], "bypassPermissions")

    def test_off_removes_mode_and_empty_block(self):
        s = cc.set_bypass({"permissions": {"defaultMode": "bypassPermissions"}}, False)
        self.assertNotIn("permissions", s)

    def test_off_keeps_other_permission_keys(self):
        s = cc.set_bypass(
            {"permissions": {"defaultMode": "bypassPermissions",
                             "allow": ["Bash(git *)"]}}, False)
        self.assertEqual(s["permissions"], {"allow": ["Bash(git *)"]})

    def test_preserves_unrelated_settings(self):
        s = cc.set_bypass({"theme": "dark"}, True)
        self.assertEqual(s["theme"], "dark")


class EnvTests(unittest.TestCase):
    def test_set(self):
        s = cc.set_env({}, "GH_TOKEN", FAKE_PAT)
        self.assertEqual(s["env"]["GH_TOKEN"], FAKE_PAT)

    def test_set_keeps_siblings(self):
        s = cc.set_env({"env": {"OTHER": "x"}}, "GH_TOKEN", FAKE_PAT)
        self.assertEqual(s["env"]["OTHER"], "x")

    def test_delete(self):
        s = cc.delete_env({"env": {"GH_TOKEN": FAKE_PAT, "OTHER": "x"}}, "GH_TOKEN")
        self.assertEqual(s["env"], {"OTHER": "x"})

    def test_delete_last_removes_block(self):
        s = cc.delete_env({"env": {"GH_TOKEN": FAKE_PAT}}, "GH_TOKEN")
        self.assertNotIn("env", s)

    def test_delete_missing_exits(self):
        with self.assertRaises(SystemExit):
            cc.delete_env({}, "NOPE")


class GitSshKeyTests(unittest.TestCase):
    def test_windows_path_forward_slashed_and_pinned(self):
        s = cc.set_git_ssh_key({}, "C:\\Users\\me\\.ssh\\id_ed25519")
        cmd = s["env"]["GIT_SSH_COMMAND"]
        self.assertIn('"C:/Users/me/.ssh/id_ed25519"', cmd)
        self.assertIn("IdentitiesOnly=yes", cmd)


class SshHostTests(unittest.TestCase):
    def test_add(self):
        s = cc.add_ssh_host({}, "box", "me@host", "C:\\k", name="My box")
        self.assertEqual(s["sshConfigs"], [{
            "id": "box", "name": "My box",
            "sshHost": "me@host", "sshIdentityFile": "C:\\k"}])

    def test_add_defaults_name_to_id(self):
        s = cc.add_ssh_host({}, "box", "me@host", "C:\\k")
        self.assertEqual(s["sshConfigs"][0]["name"], "box")

    def test_add_duplicate_id_exits(self):
        s = cc.add_ssh_host({}, "box", "me@host", "C:\\k")
        with self.assertRaises(SystemExit):
            cc.add_ssh_host(s, "box", "other@host", "C:\\k2")

    def test_remove(self):
        s = cc.add_ssh_host({}, "box", "me@host", "C:\\k")
        cc.add_ssh_host(s, "two", "me@other", "C:\\k2")
        cc.remove_ssh_host(s, "box")
        self.assertEqual([c["id"] for c in s["sshConfigs"]], ["two"])

    def test_remove_last_removes_block(self):
        s = cc.add_ssh_host({}, "box", "me@host", "C:\\k")
        cc.remove_ssh_host(s, "box")
        self.assertNotIn("sshConfigs", s)

    def test_remove_missing_exits(self):
        with self.assertRaises(SystemExit):
            cc.remove_ssh_host({}, "ghost")


class MaskingTests(unittest.TestCase):
    def test_secret_names(self):
        for name in ("GH_TOKEN", "github_pat", "API_KEY", "MY_SECRET",
                     "DB_PASSWORD", "AUTH_HEADER", "AWS_CREDENTIALS"):
            self.assertTrue(cc.is_secret_name(name), name)
        for name in ("PATH", "EDITOR", "LANG"):
            self.assertFalse(cc.is_secret_name(name), name)

    def test_mask_keeps_short_prefix_only(self):
        masked = cc.mask(FAKE_PAT)
        self.assertTrue(masked.startswith("ghp_"))
        self.assertNotIn(FAKE_PAT[4:], masked)

    def test_mask_short_values_fully(self):
        self.assertEqual(cc.mask("hunter2"), "********")

    def test_masked_view_hides_secrets_keeps_rest(self):
        settings = {"env": {"GH_TOKEN": FAKE_PAT, "EDITOR": "vim"},
                    "theme": "dark"}
        view = cc.masked_view(settings)
        self.assertNotIn(FAKE_PAT, str(view))
        self.assertEqual(view["env"]["EDITOR"], "vim")
        self.assertEqual(view["theme"], "dark")
        # original untouched
        self.assertEqual(settings["env"]["GH_TOKEN"], FAKE_PAT)


class TokenListingTests(unittest.TestCase):
    def test_only_token_like_entries_and_masked(self):
        settings = {"env": {"GH_TOKEN": FAKE_PAT, "NUGET_API_KEY": "oy_FAKEFAKE",
                            "EDITOR": "vim", "PATH": "/usr/bin"}}
        entries = cc.token_entries(settings)
        names = [n for n, _ in entries]
        self.assertIn("GH_TOKEN", names)
        self.assertIn("NUGET_API_KEY", names)
        self.assertNotIn("EDITOR", names)
        self.assertNotIn("PATH", names)
        # values are masked, never raw
        for _, masked in entries:
            self.assertNotIn(FAKE_PAT, masked)
            self.assertTrue("*" in masked)

    def test_empty_env(self):
        self.assertEqual(cc.token_entries({}), [])


class SshConfigParseTests(unittest.TestCase):
    def test_parse_blocks_and_skip_wildcard(self):
        text = SAMPLE_SSH_CONFIG.format(keydir="/keys")
        blocks = cc.parse_ssh_config(text)
        ids = [b["host"] for b in blocks]
        self.assertEqual(ids, ["github.com", "ovh"])  # '*' skipped
        gh = blocks[0]
        self.assertEqual(gh["user"], "git")
        self.assertEqual(gh["hostname"], "github.com")

    def test_host_string(self):
        block = {"host": "ovh", "hostname": "1.2.3.4", "user": "ubuntu",
                 "identityfile": "/k"}
        self.assertEqual(cc.ssh_host_string(block), "ubuntu@1.2.3.4")
        block2 = {"host": "h", "hostname": None, "user": None,
                  "identityfile": None}
        self.assertEqual(cc.ssh_host_string(block2), "h")


class SshImportTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _keys(self, *names):
        for n in names:
            open(os.path.join(self.dir, n), "w").close()

    def test_import_config_and_orphan_pub(self):
        text = SAMPLE_SSH_CONFIG.format(keydir=self.dir)
        blocks = cc.parse_ssh_config(text)
        self._keys("github.pub", "ovh_ed25519.pub", "hyperstack_canada.pub")
        pubs = [os.path.join(self.dir, p)
                for p in ("github.pub", "ovh_ed25519.pub",
                          "hyperstack_canada.pub")]
        settings, added = cc.import_ssh_entries({}, blocks, pubs)
        by_id = {c["id"]: c for c in settings["sshConfigs"]}
        # config hosts registered with real user@host
        self.assertEqual(by_id["github.com"]["sshHost"], "git@github.com")
        self.assertEqual(by_id["ovh"]["sshHost"], "ubuntu@1.2.3.4")
        # orphan pub -> placeholder
        self.assertIn("hyperstack_canada", by_id)
        self.assertEqual(by_id["hyperstack_canada"]["sshHost"], "TODO@TODO")
        # github.pub / ovh_ed25519.pub matched config keys, not re-added
        self.assertNotIn("github", by_id)
        self.assertNotIn("ovh_ed25519", by_id)
        self.assertCountEqual(added, ["github.com", "ovh", "hyperstack_canada"])

    def test_import_is_idempotent(self):
        text = SAMPLE_SSH_CONFIG.format(keydir=self.dir)
        blocks = cc.parse_ssh_config(text)
        self._keys("github.pub", "hyperstack_canada.pub")
        pubs = [os.path.join(self.dir, p)
                for p in ("github.pub", "hyperstack_canada.pub")]
        settings, _ = cc.import_ssh_entries({}, blocks, pubs)
        settings, added2 = cc.import_ssh_entries(settings, blocks, pubs)
        self.assertEqual(added2, [])

    def test_existing_entry_not_duplicated_by_identity(self):
        text = SAMPLE_SSH_CONFIG.format(keydir=self.dir)
        blocks = cc.parse_ssh_config(text)
        ident = os.path.join(self.dir, "github")
        settings = {"sshConfigs": [{
            "id": "my-github", "name": "mine",
            "sshHost": "git@github.com", "sshIdentityFile": ident}]}
        settings, added = cc.import_ssh_entries(settings, blocks, [])
        # github.com block shares the identity file -> not added again
        self.assertNotIn("github.com", added)


if __name__ == "__main__":
    unittest.main()
