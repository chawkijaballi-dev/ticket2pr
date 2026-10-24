"""Source control: local git operations and the forges' pull-request APIs."""

from ticket2pr.scm.azure import AzureDevOpsClient
from ticket2pr.scm.bitbucket import BitbucketCloudClient, BitbucketServerClient
from ticket2pr.scm.git import GitAuth, GitRepo
from ticket2pr.scm.gitea import GiteaClient
from ticket2pr.scm.github import GitHubClient
from ticket2pr.scm.gitlab import GitLabClient
from ticket2pr.scm.host import LocalHost, RepoHost

__all__ = [
    "AzureDevOpsClient",
    "BitbucketCloudClient",
    "BitbucketServerClient",
    "GitAuth",
    "GitHubClient",
    "GitLabClient",
    "GitRepo",
    "GiteaClient",
    "LocalHost",
    "RepoHost",
]
