#!/usr/bin/python
#
# Copyright 2017 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import datetime
import os
import sys
import yaml

from github import Github
from github.Gist import Gist
from github.InputFileContent import InputFileContent

from generate_bom import BomGenerator
from spinnaker.run import check_run_quick, run_quick

SERVICES = 'services'
VERSION = 'version'

COMPONENTS = [
  'clouddriver',
  'deck',
  'echo',
  'front50',
  'gate',
  'igor',
  'orca',
  'rosco',
  'fiat',
  'spinnaker-monitoring',
  'spinnaker'
]


def format_stable_branch(major, minor):
  """Provides a function to format a release branch name.

  A release branch corresponds to an execution of a release process, which
  produces some publishes some artifact (debian, container image, etc) that is
  versioned following semantic versioning. To trace the code changes included
  in a release, we create git branches to track stable releases. This function
  handles the naming of the release branch based on the release artifact's
  semantic version.

  Args:
    major [string]:  Major version of the release.
    minor [string]:  Minor version of the release.

  Returns:
    [string]: Release branch name of the form 'release-<major>.<minor>.x'.
  """
  return 'release-' + '.'.join([major, minor, 'x'])


class BomPublisher(BomGenerator):

  def __init__(self, options):
    self.__rc_version = options.rc_version
    self.__bom_dict = {}
    self.__release_version = options.release_version
    self.__gist_uri = ''
    self.__github_publisher = options.github_publisher
    self.__changelog_file = options.changelog_file
    self.__github_token = options.github_token
    self.__gist_user = options.gist_user
    self.__github = Github(self.__gist_user, self.__github_token)
    self.__patch_release = options.patch_release
    self.__alias = options.bom_alias # Flag inherited from BomGenerator.
    self.__release_name = options.release_name
    super(BomPublisher, self).__init__(options)

  def unpack_bom(self):
    """Load the release candidate BOM into memory.
    """
    bom_yaml_string = run_quick('hal version bom {0} --color false --quiet'
                                .format(self.__rc_version), echo=False).stdout.strip()
    print 'bom yaml string pulled by hal: \n\n{0}\n\n'.format(bom_yaml_string)
    self.__bom_dict = yaml.load(bom_yaml_string)
    print self.__bom_dict

  def publish_release_bom(self):
    """Read, update, and publish a release candidate BOM.
    """
    new_bom_file = '{0}.yml'.format(self.__release_version)
    self.__bom_dict[VERSION] = self.__release_version
    self.write_bom_file(new_bom_file, self.__bom_dict)
    self.publish_bom(new_bom_file)
    # Re-write the 'latest' Spinnaker version.
    if self.__alias:
      alias_file = '{0}.yml'.format(self.__alias)
      self.write_bom_file(alias_file, self.__bom_dict)
      self.publish_bom(alias_file)

    # Update the available Spinnaker versions.
    check_run_quick(
      'hal admin publish version --version {version} --alias "{alias}" --changelog {changelog}'
      .format(version=self.__release_version, alias=self.__release_name, changelog=self.__gist_uri))
    check_run_quick('hal admin publish latest {version}'
                    .format(version=self.__release_version))

  def publish_changelog_gist(self):
    """Publish the changelog as a github gist.
    """
    description = 'Changelog for Spinnaker {0}'.format(self.__release_version)
    with open(self.__changelog_file, 'r') as clog:
      raw_content_lines = clog.readlines()
      spinnaker_version = '# Spinnaker {0}\n'.format(self.__release_version)
      # Re-write the correct Spinnaker version at the top of the changelog.
      # Also add some identifying information.
      raw_content_lines = [spinnaker_version] + raw_content_lines
      timestamp = '{:%Y-%m-%d %H:%M:%S}'.format(datetime.datetime.utcnow())
      signature = '\n\nGenerated by {0} at {1}'.format(self.__github_publisher, timestamp)
      raw_content_lines.append(signature)
      content = InputFileContent(''.join(raw_content_lines))
      filename = os.path.basename(self.__changelog_file)
      gist = self.__github.get_user().create_gist(True, {filename: content}, description=description)
      self.__gist_uri = 'https://gist.github.com/{user}/{id}'.format(user=self.__gist_user, id=gist.id)
      print ('Wrote changelog to Gist at {0}.'.format(self.__gist_uri))
      # Export the changelog gist URI to include in an email notification.
      os.environ['GIST_URI'] = self.__gist_uri
      return self.__gist_uri

  def push_branch_and_tags(self):
    """Creates a release branch and pushes tags to the microservice repos owned by --github_publisher.

    A private key that has access to --github_publisher's github repos needs added
    to a running ssh-agent on the machine this script will run on:

    > <copy or rsync the key to the vm>
    > eval `ssh-agent`
    > ssh-add ~/.ssh/<key with access to github repos>

    """
    major, minor, _ = self.__release_version.split('.')

    # The stable branch will look like release-<major>.<minor>.x since nebula
    # enforces restrictions on what branches it does releases from.
    # https://github.com/nebula-plugins/nebula-release-plugin#extension-provided
    stable_branch = format_stable_branch(major, minor)
    for comp in COMPONENTS:
      comp_path = os.path.join(self.base_dir, comp)
      if self.__patch_release:
        check_run_quick('git -C {0} checkout {1}'.format(comp_path, stable_branch))
      else:
        # Create new release branch.
        check_run_quick('git -C {0} checkout -b {1}'.format(comp_path, stable_branch))

      version_tag_build = ''
      if comp == 'spinnaker-monitoring':
        version_tag_build = 'version-{0}'.format(self.__bom_dict[SERVICES]['monitoring-daemon'][VERSION])
      else:
        version_tag_build = 'version-{0}'.format(self.__bom_dict[SERVICES][comp][VERSION])

      last_dash = version_tag_build.rindex('-')
      version_tag = version_tag_build[:last_dash]
      repo_to_push = ('git@github.com:{owner}/{comp}.git'
                      .format(owner=self.__github_publisher, comp=comp))
      check_run_quick('git -C {comp} remote add release {url}'
                      .format(comp=comp_path, url=repo_to_push))
      check_run_quick('git -C {comp} push release {branch}'
                      .format(comp=comp_path, branch=stable_branch))

      repo = self.__github.get_repo('{owner}/{comp}'.format(owner=self.__github_publisher, comp=comp))
      paginated_tags = repo.get_tags()
      tag_names = [tag.name for tag in paginated_tags]
      if version_tag not in tag_names:
        # The tag doesn't exist and we need to push a tag.
        print ('pushing version tag {tag} to {owner}/{comp}'
               .format(tag=version_tag, owner=self.__github_publisher, comp=comp))
        check_run_quick('git -C {comp} push release {tag}'
                        .format(comp=comp_path,  tag=version_tag))
      # Clean up git artifacts specific to this publication.
      check_run_quick('git -C {comp} remote remove release'
                      .format(comp=comp_path))

  @classmethod
  def main(cls):
    parser = argparse.ArgumentParser()
    cls.init_argument_parser(parser)
    options = parser.parse_args()

    bom_publisher = cls(options)
    bom_publisher.unpack_bom()
    bom_publisher.publish_changelog_gist()
    bom_publisher.push_branch_and_tags()
    bom_publisher.publish_release_bom()

  @classmethod
  def init_argument_parser(cls, parser):
    """Initialize command-line arguments."""
    parser.add_argument('--changelog_file', default='', required=True,
                        help='The changelog to publish during this publication.')
    parser.add_argument('--github_publisher', default='', required=True,
                        help="The owner of the remote repo the branch and tag are pushed to for each component.")
    parser.add_argument('--github_token', default='', required=True,
                        help="The GitHub user token with scope='gists' to write gists.")
    parser.add_argument('--gist_user', default='', required=True,
                        help="The GitHub user to write gists as.")
    parser.add_argument('--patch_release', default=False, action='store_true',
                        help='Make a patch release.')
    parser.add_argument('--rc_version', default='', required=True,
                        help='The version of the Spinnaker release candidate we are publishing.')
    parser.add_argument('--release_name', default='', required=True,
                        help="The name for the new Spinnaker release.")
    parser.add_argument('--release_version', default='', required=True,
                        help="The version for the new Spinnaker release. This needs to be of the form 'X.Y.Z'.")
    parser.add_argument('--changelog_gist_only', default=False, action='store_true',
                        help="Publish the changelog as a gist, but don't publish the actual release.")
    super(BomPublisher, cls).init_argument_parser(parser)

if __name__ == '__main__':
  sys.exit(BomPublisher.main())
