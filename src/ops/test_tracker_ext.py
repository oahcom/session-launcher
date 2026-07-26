"""Tests for tracker._get_collab_mode — mode-aware collaboration detection."""

from unittest.mock import patch

import pytest

from ops.tracker import _get_collab_mode


def _fake_role_json(workgroup):
    """Build a fake persona dict with the given workgroup list."""
    return {"name": "test_role", "workgroup": workgroup}


class TestGetCollabMode:
    """_get_collab_mode reads `workgroup` list from the role JSON and
    returns `mode` for the matching partner entry."""

    @patch("ops.tracker._get_role_json")
    def test_get_collab_mode_master_slave(self, mock_get):
        """workgroup has a master-slave mode entry for the partner."""
        mock_get.return_value = _fake_role_json([
            {"role": "partner_a", "mode": "master-slave"},
        ])
        assert _get_collab_mode("any_role", "partner_a") == "master-slave"

    @patch("ops.tracker._get_role_json")
    def test_get_collab_mode_peer(self, mock_get):
        """workgroup has a peer-to-peer mode entry for the partner."""
        mock_get.return_value = _fake_role_json([
            {"role": "partner_a", "mode": "peer-to-peer"},
        ])
        assert _get_collab_mode("any_role", "partner_a") == "peer-to-peer"

    @patch("ops.tracker._get_role_json")
    def test_get_collab_mode_notify(self, mock_get):
        """workgroup has a notify-only mode entry for the partner."""
        mock_get.return_value = _fake_role_json([
            {"role": "partner_a", "mode": "notify-only"},
        ])
        assert _get_collab_mode("any_role", "partner_a") == "notify-only"

    @patch("ops.tracker._get_role_json")
    def test_get_collab_mode_default(self, mock_get):
        """workgroup has no entry for the partner — falls back to peer-to-peer."""
        mock_get.return_value = _fake_role_json([
            {"role": "some_other_partner", "mode": "master-slave"},
        ])
        assert _get_collab_mode("any_role", "unknown_partner") == "peer-to-peer"

    @patch("ops.tracker._get_role_json")
    def test_get_collab_mode_no_json(self, mock_get):
        """_get_role_json returns None — falls back to peer-to-peer."""
        mock_get.return_value = None
        assert _get_collab_mode("any_role", "any_partner") == "peer-to-peer"
