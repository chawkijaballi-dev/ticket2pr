"""Issue tracker integrations."""

from ticket2pr.trackers.base import Tracker
from ticket2pr.trackers.jira import JiraTracker
from ticket2pr.trackers.local import LocalTracker

__all__ = ["JiraTracker", "LocalTracker", "Tracker"]
