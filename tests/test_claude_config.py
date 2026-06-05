"""Unit tests for claude_config.py - pure mutators and masking, no file I/O.

All token values here are made up; never put a real credential in a test.
"""

import unittest

import claude_config as cc

FAKE_PAT = "ghp_FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE0000"


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


if __name__ == "__main__":
    unittest.main()
