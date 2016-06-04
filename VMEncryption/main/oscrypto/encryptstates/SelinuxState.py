#!/usr/bin/env python
#
# VM Backup extension
#
# Copyright 2015 Microsoft Corporation
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
#
# Requires Python 2.7+
#

from OSEncryptionState import *

class SelinuxState(OSEncryptionState):
    def __init__(self, context):
        super(SelinuxState, self).__init__('SelinuxState', context)

    def enter(self):
        if not super(SelinuxState, self).should_enter():
            return

        self.context.logger.log("Entering selinux state")

    def should_exit(self):
        self.context.logger.log("Verifying if machine should exit selinux state")

        return super(SelinuxState, self).should_exit()
