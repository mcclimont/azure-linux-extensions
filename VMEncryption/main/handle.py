﻿#!/usr/bin/env python
#
# VMEncryption extension
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

import array
import base64
import httplib
import imp
import json
import os
import os.path
import re
import shlex
import string
import subprocess
import sys
import datetime
import time
import tempfile
import traceback
import urllib2
import urlparse
import uuid

from Utils import HandlerUtil
from Common import *
from ExtensionParameter import ExtensionParameter
from DiskUtil import DiskUtil
from BackupLogger import BackupLogger
from KeyVaultUtil import KeyVaultUtil
from EncryptionConfig import *
from patch import *
from BekUtil import *
from EncryptionMarkConfig import EncryptionMarkConfig
from EncryptionEnvironment import EncryptionEnvironment
from MachineIdentity import MachineIdentity
from OnGoingItemConfig import OnGoingItemConfig
from __builtin__ import int
#Main function is the only entrence to this extension handler
def install():
    hutil.do_parse_context('Install')
    hutil.do_exit(0, 'Install', CommonVariables.extension_success_status, str(CommonVariables.success), 'Install Succeeded')

def uninstall():
    hutil.do_parse_context('Uninstall')
    hutil.do_exit(0,'Uninstall',CommonVariables.extension_success_status,'0', 'Uninstall succeeded')

def disable():
    hutil.do_parse_context('Disable')
    hutil.do_exit(0,'Disable',CommonVariables.extension_success_status,'0', 'Disable Succeeded')

def update():
    hutil.do_parse_context('Upadate')
    hutil.do_exit(0,'Update',CommonVariables.extension_success_status,'0', 'Update Succeeded')

def exit_without_status_report():
    sys.exit(0)

def not_support_header_option_distro(patching):
    if(patching.distro_info[0].lower() == "centos" and patching.distro_info[1].startswith('6.')):
        return True
    if(patching.distro_info[0].lower() == "redhat" and patching.distro_info[1].startswith('6.')):
        return True
    if(patching.distro_info[0].lower() == "suse" and patching.distro_info[1].startswith('11')):
        return True
    return False

def none_or_empty(obj):
    if(obj is None or obj == ""):
        return True
    else:
        return False

def toggle_se_linux_for_centos7(disable):
    if(MyPatching.distro_info[0].lower() == 'centos' and MyPatching.distro_info[1].startswith('7.0')):
        if(disable):
            se_linux_status = encryption_environment.get_se_linux()
            if(se_linux_status.lower() == 'enforcing'):
                encryption_environment.disable_se_linux()
                return True
        else:
            encryption_environment.enable_se_linux()
    return False

def mount_encrypted_disks(disk_util, bek_util,passphrase_file,encryption_config):
    #make sure the azure disk config path exists.
    crypt_items = disk_util.get_crypt_items()
    if(crypt_items is not None):
        for i in range(0, len(crypt_items)):
            crypt_item = crypt_items[i]
            #add walkaround for the centos 7.0
            se_linux_status = None
            if(MyPatching.distro_info[0].lower() == 'centos' and MyPatching.distro_info[1].startswith('7.0')):
                se_linux_status = encryption_environment.get_se_linux()
                if(se_linux_status.lower() == 'enforcing'):
                    encryption_environment.disable_se_linux()
            luks_open_result = disk_util.luks_open(passphrase_file=passphrase_file,dev_path=crypt_item.dev_path,mapper_name=crypt_item.mapper_name,header_file=crypt_item.luks_header_path)
            logger.log("luks open result is " + str(luks_open_result))
            if(MyPatching.distro_info[0].lower() == 'centos' and MyPatching.distro_info[1].startswith('7.0')):
                if(se_linux_status is not None and se_linux_status.lower() == 'enforcing'):
                    encryption_environment.enable_se_linux()
            if(crypt_item.mount_point != 'None'):
                disk_util.mount_crypt_item(crypt_item, passphrase_file)
            else:
                logger.log(msg=('mount_point is None so skipping mount for the item ' + str(crypt_item)),level=CommonVariables.WarningLevel)
    bek_util.umount_azure_passhprase(encryption_config)

def main():
    global hutil,MyPatching,logger,encryption_environment
    HandlerUtil.LoggerInit('/var/log/waagent.log','/dev/stdout')
    HandlerUtil.waagent.Log("%s started to handle." % (CommonVariables.extension_name))
    
    hutil = HandlerUtil.HandlerUtility(HandlerUtil.waagent.Log, HandlerUtil.waagent.Error, CommonVariables.extension_name)
    logger = BackupLogger(hutil)
    MyPatching = GetMyPatching(logger)
    hutil.patching = MyPatching

    encryption_environment = EncryptionEnvironment(patching=MyPatching,logger=logger)
    if MyPatching is None:
        hutil.do_exit(0, 'Enable', CommonVariables.extension_error_status, str(CommonVariables.os_not_supported), 'the os is not supported')

    for a in sys.argv[1:]:
        if re.match("^([-/]*)(disable)", a):
            disable()
        elif re.match("^([-/]*)(uninstall)", a):
            uninstall()
        elif re.match("^([-/]*)(install)", a):
            install()
        elif re.match("^([-/]*)(enable)", a):
            enable()
        elif re.match("^([-/]*)(update)", a):
            update()
        elif re.match("^([-/]*)(daemon)", a):
            daemon()

def mark_encryption(command,volume_type,disk_format_query):
    encryption_marker = EncryptionMarkConfig(logger, encryption_environment)
    encryption_marker.command = command
    encryption_marker.volume_type = volume_type
    encryption_marker.diskFormatQuery = disk_format_query
    encryption_marker.commit()
    return encryption_marker

def enable():
    hutil.do_parse_context('Enable')
    # we need to start another subprocess to do it, because the initial process
    # would be killed by the wala in 5 minutes.
    logger.log('enabling...')

    """
    trying to mount the crypted items.
    """
    disk_util = DiskUtil(hutil = hutil, patching = MyPatching, logger = logger, encryption_environment = encryption_environment)
    bek_util = BekUtil(disk_util, logger)
    
    existed_passphrase_file = None
    encryption_config = EncryptionConfig(encryption_environment=encryption_environment, logger = logger)
    config_path_result = disk_util.make_sure_path_exists(encryption_environment.encryption_config_path)
    if(config_path_result != CommonVariables.process_success):
        logger.log(msg="azure encryption path creation failed.",level=CommonVariables.ErrorLevel)
    if(encryption_config.config_file_exists()):
        existed_passphrase_file = bek_util.get_bek_passphrase_file(encryption_config)
        if(existed_passphrase_file is not None):
            mount_encrypted_disks(disk_util=disk_util,bek_util=bek_util,encryption_config=encryption_config,passphrase_file=existed_passphrase_file)
        else:
            logger.log(msg="the config is there, but we could not get the bek file.",level=CommonVariables.WarningLevel)
            exit_without_status_report()

    # handle the re-call scenario.  the re-call would resume?
    # if there's one tag for the next reboot.
    encryption_marker = EncryptionMarkConfig(logger, encryption_environment)
    if (not encryption_marker.config_file_exists()):
        machine_identity = MachineIdentity()
        stored_identity = machine_identity.stored_identity()
        if(stored_identity is None):
            machine_identity.save_identity()
        else:
            current_identity = machine_identity.current_identity()
            if(current_identity != stored_identity):
                current_seq_no = -1
                backup_logger.log("machine identity not same, set current_seq_no to " + str(current_seq_no) + " " + str(stored_identity) + " " + str(current_identity), True)
                hutil.set_last_seq(current_seq_no)
                machine_identity.save_identity()
                # we should be careful about proceed for this case, we just
                # failed this time to wait for customers' retry.
                exit_without_status_report()

    hutil.exit_if_same_seq()
    hutil.save_seq()

    try:
        protected_settings_str = hutil._context._config['runtimeSettings'][0]['handlerSettings'].get('protectedSettings')
        public_settings_str = hutil._context._config['runtimeSettings'][0]['handlerSettings'].get('publicSettings')
        if(isinstance(public_settings_str,basestring)):
            public_settings = json.loads(public_settings_str)
        else:
            public_settings = public_settings_str;

        if(isinstance(protected_settings_str,basestring)):
            protected_settings = json.loads(protected_settings_str)
        else:
            protected_settings = protected_settings_str
        extension_parameter = ExtensionParameter(hutil, protected_settings, public_settings)
        
        kek_secret_id_created = None

        encryption_marker = EncryptionMarkConfig(logger, encryption_environment)
        if encryption_marker.config_file_exists():
            # verify the encryption mark
            logger.log(msg="encryption mark is there, starting daemon.",level=CommonVariables.InfoLevel)
            start_daemon()
        else:
            if(encryption_config.config_file_exists() and existed_passphrase_file is not None):
                logger.log(msg="config file exists and passphrase file exists.", level=CommonVariables.WarningLevel)
                encryption_marker = mark_encryption(command=extension_parameter.command, \
                                                  volume_type=extension_parameter.VolumeType, \
                                                  disk_format_query=extension_parameter.DiskFormatQuery)
                start_daemon()
            else:
                """
                creating the secret, the secret would be transferred to a bek volume after the updatevm called in powershell.
                """
                #store the luks passphrase in the secret.
                keyVaultUtil = KeyVaultUtil(logger)

                """
                validate the parameters
                """
                if(extension_parameter.VolumeType is None or extension_parameter.VolumeType.lower() != 'data'):
                    hutil.do_exit(0, 'Enable', CommonVariables.extension_error_status,str(CommonVariables.volue_type_not_support), 'VolumeType ' + str(extension_parameter.VolumeType) + ' is not supported.')

                if(extension_parameter.command not in [CommonVariables.EnableEncryption, CommonVariables.EnableEncryptionFormat]):
                    hutil.do_exit(0, 'Enable', CommonVariables.extension_error_status,str(CommonVariables.command_not_support), 'Command ' + str(extension_parameter.command) + ' is not supported.')

                """
                this is the fresh call case
                """
                #handle the passphrase related
                if(existed_passphrase_file is None):
                    if(extension_parameter.passphrase is None or extension_parameter.passphrase == ""):
                        extension_parameter.passphrase = bek_util.generate_passphrase(extension_parameter.KeyEncryptionAlgorithm)
                    else:
                        logger.log(msg="the extension_parameter.passphrase is none")

                    kek_secret_id_created = keyVaultUtil.create_kek_secret(Passphrase = extension_parameter.passphrase,\
                    KeyVaultURL = extension_parameter.KeyVaultURL,\
                    KeyEncryptionKeyURL = extension_parameter.KeyEncryptionKeyURL,\
                    AADClientID = extension_parameter.AADClientID,\
                    KeyEncryptionAlgorithm = extension_parameter.KeyEncryptionAlgorithm,\
                    AADClientSecret = extension_parameter.AADClientSecret,\
                    DiskEncryptionKeyFileName = extension_parameter.DiskEncryptionKeyFileName)

                    if(kek_secret_id_created is None):
                        hutil.do_exit(0, 'Enable', CommonVariables.extension_error_status, str(CommonVariables.create_encryption_secret_failed), 'Enable failed.')
                    else:
                        encryption_config.passphrase_file_name = extension_parameter.DiskEncryptionKeyFileName
                        encryption_config.bek_filesystem = CommonVariables.BekVolumeFileSystem
                        encryption_config.secret_id = kek_secret_id_created
                        encryption_config.commit()
   
                encryption_marker = mark_encryption(command=extension_parameter.command, \
                                                  volume_type=extension_parameter.VolumeType, \
                                                  disk_format_query=extension_parameter.DiskFormatQuery)

                if(kek_secret_id_created != None):
                    hutil.do_exit(0, 'Enable', CommonVariables.extension_success_status, str(CommonVariables.success), str(kek_secret_id_created))
                else:
                    """
                    the enabling called again. the passphrase would be re-used.
                    """
                    hutil.do_exit(0, 'Enable', CommonVariables.extension_success_status, str(CommonVariables.encrypttion_already_enabled), str(kek_secret_id_created))
    except Exception as e:
        logger.log(msg="Failed to enable the extension with error: %s, stack trace: %s" % (str(e), traceback.format_exc()),level=CommonVariables.ErrorLevel)
        hutil.do_exit(0, 'Enable',CommonVariables.extension_error_status,str(CommonVariables.unknown_error), 'Enable failed.')

def enable_encryption_format(passphrase, encryption_marker, disk_util):
    encryption_parameters = encryption_marker.get_encryption_disk_format_query()

    encryption_format_items = json.loads(encryption_parameters)
    for encryption_item in encryption_format_items:
        sdx_path = disk_util.query_dev_sdx_path_by_scsi_id(encryption_item["scsi"])
        devices = disk_util.get_device_items(sdx_path)
        if(len(devices) != 1):
            logger.log(msg=("the device with scsi number:" + str(encryption_item["scsi"]) + " have more than one sub device. so skip it."),level=CommonVariables.WarningLevel)
            continue
        else:
            device_item = devices[0]
            if(device_item.file_system == "" and device_item.type == "disk"):
                mapper_name = str(uuid.uuid4())
                logger.log("encrypting " + str(device_item))
                device_to_encrypt_uuid_path = os.path.join("/dev/disk/by-uuid", device_item.uuid)
                encrypted_device_path = os.path.join(CommonVariables.dev_mapper_root,mapper_name)
                try:
                    se_linux_status = None
                    if(MyPatching.distro_info[0].lower() == 'centos' and MyPatching.distro_info[1].startswith('7.0')):
                        se_linux_status = encryption_environment.get_se_linux()
                        if(se_linux_status.lower() == 'enforcing'):
                            encryption_environment.disable_se_linux()
                    encrypt_result = disk_util.encrypt_disk(dev_path = device_to_encrypt_uuid_path, passphrase_file = passphrase, mapper_name = mapper_name, header_file=None)
                finally:
                    if(MyPatching.distro_info[0].lower() == 'centos' and MyPatching.distro_info[1].startswith('7.0')):
                        if(se_linux_status is not None and se_linux_status.lower() == 'enforcing'):
                            encryption_environment.enable_se_linux()

                if(encrypt_result == CommonVariables.process_success):
                    #TODO: let customer specify it in the parameter
                    file_system = CommonVariables.default_file_system
                    format_disk_result = disk_util.format_disk(dev_path = encrypted_device_path,file_system = file_system)
                    if(format_disk_result != CommonVariables.process_success):
                        logger.log(msg = ("format disk " + str(encrypted_device_path) + " failed " + str(format_disk_result)),level = CommonVariables.ErrorLevel)
                    crypt_item_to_update = CryptItem()
                    crypt_item_to_update.mapper_name = mapper_name
                    crypt_item_to_update.dev_path = device_to_encrypt_uuid_path
                    crypt_item_to_update.luks_header_path = "None"
                    crypt_item_to_update.file_system = file_system

                    if(encryption_item["name"] is not None):
                        crypt_item_to_update.mount_point = os.path.join("/mnt/", str(encryption_item["name"]))
                    else:
                        crypt_item_to_update.mount_point = os.path.join("/mnt/", mapper_name)

                    disk_util.make_sure_path_exists(crypt_item_to_update.mount_point)
                    update_crypt_item_result = disk_util.update_crypt_item(crypt_item_to_update)
                    if(not update_crypt_item_result):
                        logger.log(msg="update crypt item failed",level=CommonVariables.ErrorLevel)

                    mount_result = disk_util.mount_filesystem(dev_path=encrypted_device_path,mount_point=crypt_item_to_update.mount_point)
                    logger.log(msg=("mount result is " + str(mount_result)))
                else:
                    logger.log(msg="encryption failed with code " + str(encrypt_result),level=CommonVariables.ErrorLevel)
            else:
                logger.log(msg="the item fstype is not empty or the type is not a disk")

def encrypt_inplace_without_seperate_header_file(passphrase_file, device_item, disk_util, bek_util, ongoing_item_config=None):
    """
    if ongoing_item_config is not None, then this is a resume case.
    this function will return the phase 
    """
    current_phase = CommonVariables.EncryptionPhaseBackupHeader
    if(ongoing_item_config is None):
        ongoing_item_config = OnGoingItemConfig(encryption_environment = encryption_environment, logger = logger)
        ongoing_item_config.current_block_size = CommonVariables.default_block_size
        ongoing_item_config.current_slice_index = 0
        ongoing_item_config.device_size = device_item.size
        ongoing_item_config.file_system = device_item.file_system
        ongoing_item_config.luks_header_file_path = None
        ongoing_item_config.mapper_name = str(uuid.uuid4())
        ongoing_item_config.mount_point = device_item.mount_point
        ongoing_item_config.original_dev_name_path = os.path.join('/dev', device_item.name)
        ongoing_item_config.original_dev_path = os.path.join('/dev', device_item.name)
        ongoing_item_config.phase = CommonVariables.EncryptionPhaseBackupHeader
        ongoing_item_config.commit()
    else:
        logger.log(msg = "ongoing item config is not none, this is resuming: " + str(ongoing_item_config), level = CommonVariables.WarningLevel)

    logger.log(msg=("encrypting device item:" + str(ongoing_item_config.get_original_dev_path())))
    # we only support ext file systems.
    current_phase = ongoing_item_config.get_phase()

    original_dev_path = ongoing_item_config.get_original_dev_path()
    mapper_name = ongoing_item_config.get_mapper_name()
    device_size = ongoing_item_config.get_device_size()

    luks_header_size = CommonVariables.luks_header_size
    size_shrink_to = (device_size - luks_header_size) / CommonVariables.sector_size

    while(current_phase != CommonVariables.EncryptionPhaseDone):
        if(current_phase == CommonVariables.EncryptionPhaseBackupHeader):
            if(not ongoing_item_config.get_file_system().lower() in ["ext2","ext3","ext4"]):
                logger.log(msg = "we only support ext file systems for centos 6.5/6.6/6.7 and redhat 6.7", level = CommonVariables.WarningLevel)
                return current_phase

            chk_shrink_result = disk_util.check_shrink_fs(dev_path = original_dev_path,size_shrink_to = size_shrink_to)
            if(chk_shrink_result != CommonVariables.process_success):
                logger.log(msg = ("check shrink fs failed with code " + str(chk_shrink_result) + " for: " + str(original_dev_path)), level = CommonVariables.ErrorLevel)
                logger.log(msg = "your file system may not have enough space to do the encryption.")
                return current_phase
            else:
                ongoing_item_config.current_slice_index = 0
                ongoing_item_config.current_source_path = original_dev_path
                ongoing_item_config.current_destination = encryption_environment.copy_header_slice_file_path
                ongoing_item_config.current_total_copy_size = CommonVariables.default_block_size
                ongoing_item_config.from_end = False
                ongoing_item_config.header_slice_file_path = encryption_environment.copy_header_slice_file_path
                ongoing_item_config.original_dev_path = original_dev_path
                ongoing_item_config.commit()
                if(os.path.exists(encryption_environment.copy_header_slice_file_path)):
                    os.remove(encryption_environment.copy_header_slice_file_path)

                copy_result = disk_util.copy(ongoing_item_config = ongoing_item_config)

                if(copy_result != CommonVariables.process_success):
                    logger.log(msg=("copy the header block failed, return code is: " + str(copy_result)),level=CommonVariables.ErrorLevel)
                    return current_phase
                else:
                    ongoing_item_config.current_slice_index = 0
                    ongoing_item_config.phase = CommonVariables.EncryptionPhaseEncryptDevice
                    ongoing_item_config.commit()
                    current_phase = CommonVariables.EncryptionPhaseEncryptDevice
        elif(current_phase == CommonVariables.EncryptionPhaseEncryptDevice):
            encrypt_result = disk_util.encrypt_disk(dev_path = original_dev_path, passphrase_file = passphrase_file, mapper_name = mapper_name, header_file = None)
            # after the encrypt_disk without seperate header, then the uuid
            # would change.
            if(encrypt_result != CommonVariables.process_success):
                logger.log(msg = "encrypt file system failed.", level = CommonVariables.ErrorLevel)
                return current_phase
            else:
                ongoing_item_config.current_slice_index = 0
                ongoing_item_config.phase = CommonVariables.EncryptionPhaseCopyData
                ongoing_item_config.commit()
                current_phase = CommonVariables.EncryptionPhaseCopyData

        elif(current_phase == CommonVariables.EncryptionPhaseCopyData):
            device_mapper_path = os.path.join(CommonVariables.dev_mapper_root, mapper_name)
            ongoing_item_config.current_destination = device_mapper_path
            ongoing_item_config.current_source_path = original_dev_path
            ongoing_item_config.current_total_copy_size = (device_size - luks_header_size)
            ongoing_item_config.from_end = True
            ongoing_item_config.phase = CommonVariables.EncryptionPhaseCopyData
            ongoing_item_config.commit()

            copy_result = disk_util.copy(ongoing_item_config = ongoing_item_config)
            if(copy_result != CommonVariables.process_success):
                logger.log(msg = ("copy the main content block failed, return code is: " + str(copy_result)),level = CommonVariables.ErrorLevel)
                return current_phase
            else:
                ongoing_item_config.phase = CommonVariables.EncryptionPhaseRecoverHeader
                ongoing_item_config.commit()
                current_phase = CommonVariables.EncryptionPhaseRecoverHeader

        elif(current_phase == CommonVariables.EncryptionPhaseRecoverHeader):
            ongoing_item_config.from_end = False
            backed_up_header_slice_file_path = ongoing_item_config.get_header_slice_file_path()
            ongoing_item_config.current_slice_index = 0
            ongoing_item_config.current_source_path = backed_up_header_slice_file_path
            device_mapper_path = os.path.join(CommonVariables.dev_mapper_root, mapper_name)
            ongoing_item_config.current_destination = device_mapper_path
            ongoing_item_config.current_total_copy_size = CommonVariables.default_block_size
            ongoing_item_config.commit()

            copy_result = disk_util.copy(ongoing_item_config = ongoing_item_config)
            if(copy_result == CommonVariables.process_success):
                crypt_item_to_update = CryptItem()
                crypt_item_to_update.mapper_name = mapper_name
                original_dev_name_path = ongoing_item_config.get_original_dev_name_path()
                crypt_item_to_update.dev_path = original_dev_name_path
                crypt_item_to_update.luks_header_path = "None"
                crypt_item_to_update.file_system = ongoing_item_config.get_file_system()
                # if the original mountpoint is empty, then leave
                # it as None
                mount_point = ongoing_item_config.get_mount_point()
                if mount_point == "" or mount_point is None:
                    crypt_item_to_update.mount_point = "None"
                else:
                    crypt_item_to_update.mount_point = mount_point
                update_crypt_item_result = disk_util.update_crypt_item(crypt_item_to_update)
                if(not update_crypt_item_result):
                    logger.log(msg="update crypt item failed",level = CommonVariables.ErrorLevel)

                if(os.path.exists(encryption_environment.copy_header_slice_file_path)):
                    os.remove(encryption_environment.copy_header_slice_file_path)

                current_phase = CommonVariables.EncryptionPhaseDone
                ongoing_item_config.phase = current_phase
                ongoing_item_config.commit()
                expand_fs_result = disk_util.expand_fs(dev_path=device_mapper_path)

                if(crypt_item_to_update.mount_point != "None"):
                    disk_util.mount_filesystem(device_mapper_path, ongoing_item_config.get_mount_point())
                else:
                    logger.log("the crypt_item_to_update.mount_point is None, so we do not mount it.")

                ongoing_item_config.clear_config()
                if(expand_fs_result != CommonVariables.process_success):
                    logger.log(msg=("expand fs result is: " + str(expand_fs_result)),level = CommonVariables.ErrorLevel)
                return current_phase
            else:
                logger.log(msg=("recover header failed result is: " + str(copy_result)),level = CommonVariables.ErrorLevel)
                return current_phase

def encrypt_inplace_with_seperate_header_file(passphrase_file, device_item, disk_util, bek_util, ongoing_item_config=None):
    """
    if ongoing_item_config is not None, then this is a resume case.
    """
    current_phase = CommonVariables.EncryptionPhaseEncryptDevice
    if(ongoing_item_config is None):
        ongoing_item_config = OnGoingItemConfig(encryption_environment=encryption_environment,logger=logger)
        mapper_name = str(uuid.uuid4())
        ongoing_item_config.current_block_size = CommonVariables.default_block_size
        ongoing_item_config.current_slice_index = 0
        ongoing_item_config.device_size = device_item.size
        ongoing_item_config.file_system = device_item.file_system
        ongoing_item_config.mapper_name = mapper_name
        ongoing_item_config.mount_point = device_item.mount_point
        ongoing_item_config.mount_point = device_item.mount_point
        ongoing_item_config.original_dev_name_path = os.path.join('/dev/', device_item.name)
        ongoing_item_config.original_dev_path = os.path.join('/dev/disk/by-uuid', device_item.uuid)
        luks_header_file = disk_util.create_luks_header(mapper_name=mapper_name)
        if(luks_header_file is None):
            logger.log(msg="create header file failed", level=CommonVariables.ErrorLevel)
            return current_phase
        else:
            ongoing_item_config.luks_header_file_path = luks_header_file
            ongoing_item_config.phase = CommonVariables.EncryptionPhaseEncryptDevice
            ongoing_item_config.commit()
    else:
        logger.log(msg = "ongoing item config is not none, this is resuming: " + str(ongoing_item_config),level = CommonVariables.WarningLevel)
        current_phase = ongoing_item_config.get_phase()

    while(current_phase != CommonVariables.EncryptionPhaseDone):
        if(current_phase == CommonVariables.EncryptionPhaseEncryptDevice):
            disabled = False
            try:
                mapper_name = ongoing_item_config.get_mapper_name()
                original_dev_path = ongoing_item_config.get_original_dev_path()
                luks_header_file = ongoing_item_config.get_header_file_path()
                disabled = toggle_se_linux_for_centos7(True)
                encrypt_result = disk_util.encrypt_disk(dev_path = original_dev_path, passphrase_file = passphrase_file, \
                                                        mapper_name = mapper_name, header_file = luks_header_file)
                if(encrypt_result != CommonVariables.process_success):
                    logger.log(msg=("the encrypton for " + str(original_dev_path) + " failed"),level=CommonVariables.ErrorLevel)
                    return current_phase
                else:
                    ongoing_item_config.phase = CommonVariables.EncryptionPhaseCopyData
                    ongoing_item_config.commit()
                    current_phase = CommonVariables.EncryptionPhaseCopyData
            finally:
                toggle_se_linux_for_centos7(False)

        elif(current_phase == CommonVariables.EncryptionPhaseCopyData):
            disabled = False
            try:
                mapper_name = ongoing_item_config.get_mapper_name()
                original_dev_path = ongoing_item_config.get_original_dev_path()
                luks_header_file = ongoing_item_config.get_header_file_path()
                disabled = toggle_se_linux_for_centos7(True)
                device_mapper_path = os.path.join("/dev/mapper", mapper_name)
                if(not os.path.exists(device_mapper_path)):
                    open_result = disk_util.luks_open(passphrase_file = passphrase_file, dev_path = original_dev_path, \
                                                            mapper_name = mapper_name, header_file = luks_header_file)

                    if(open_result != CommonVariables.process_success):
                        logger.log(msg=("the luks open for " + str(original_dev_path) + " failed"),level = CommonVariables.ErrorLevel)
                        return current_phase
                else:
                    logger.log(msg = "the device mapper path existed, so skip the luks open.", level = CommonVariables.InfoLevel)

                device_size = ongoing_item_config.get_device_size()

                current_slice_index = ongoing_item_config.get_current_slice_index()
                if(current_slice_index is None):
                    ongoing_item_config.current_slice_index = 0
                ongoing_item_config.current_source_path = original_dev_path
                ongoing_item_config.current_destination = device_mapper_path
                ongoing_item_config.current_total_copy_size = device_size
                ongoing_item_config.from_end = True
                ongoing_item_config.commit()

                copy_result = disk_util.copy(ongoing_item_config = ongoing_item_config)
                if(copy_result != CommonVariables.success):
                    error_message = "the copying result is " + str(copy_result) + " so skip the mounting"
                    logger.log(msg = (error_message), level = CommonVariables.ErrorLevel)
                    return current_phase
                else:
                    crypt_item_to_update = CryptItem()
                    crypt_item_to_update.mapper_name = mapper_name
                    original_dev_name_path = ongoing_item_config.get_original_dev_name_path()
                    crypt_item_to_update.dev_path = original_dev_name_path
                    crypt_item_to_update.luks_header_path = luks_header_file
                    crypt_item_to_update.file_system = ongoing_item_config.get_file_system()
                    # if the original mountpoint is empty, then leave
                    # it as None
                    mount_point = ongoing_item_config.get_mount_point()
                    if mount_point == "" or mount_point is None:
                        crypt_item_to_update.mount_point = "None"
                    else:
                        crypt_item_to_update.mount_point = mount_point
                    update_crypt_item_result = disk_util.update_crypt_item(crypt_item_to_update)
                    if(not update_crypt_item_result):
                        logger.log(msg="update crypt item failed",level = CommonVariables.ErrorLevel)
                    if(crypt_item_to_update.mount_point != "None"):
                        disk_util.mount_filesystem(device_mapper_path, mount_point)
                    else:
                        logger.log("the crypt_item_to_update.mount_point is None, so we do not mount it.")
                    current_phase = CommonVariables.EncryptionPhaseDone
                    ongoing_item_config.phase = current_phase
                    ongoing_item_config.commit()
                    ongoing_item_config.clear_config()
                    return current_phase
            finally:
                toggle_se_linux_for_centos7(False)

def enable_encryption_all_in_place(passphrase_file, encryption_marker, disk_util, bek_util):
    """
    if return None for the success case, or return the device item which failed.
    """
    logger.log(msg="executing the enableencryption_all_inplace command.")
    device_items = disk_util.get_device_items(None)
    encrypted_items = []
    error_message = ""
    for device_item in device_items:
        logger.log("device_item == " + str(device_item))

        should_skip = disk_util.should_skip_for_inplace_encryption(device_item)
        if(not should_skip):
            if(device_item.name == bek_util.passphrase_device):
                logger.log("skip for the passphrase disk " + str(device_item))
                should_skip = True
            if(device_item.uuid in encrypted_items):
                logger.log("already did a operation " + str(device_item) + " so skip it")
                should_skip = True
        if(not should_skip):
            umount_status_code = CommonVariables.success
            if(device_item.mount_point is not None and device_item.mount_point != ""):
                umount_status_code = disk_util.umount(device_item.mount_point)
            if(umount_status_code != CommonVariables.success):
                logger.log("error occured when do the umount for " + str(device_item.mount_point) + str(umount_status_code))
            else:
                encrypted_items.append(device_item.uuid)
                logger.log(msg=("encrypting " + str(device_item)))
                no_header_file_support = not_support_header_option_distro(MyPatching)
                #TODO check the file system before encrypting it.
                if(no_header_file_support):
                    logger.log(msg="this is the centos 6 or redhat 6 or sles 11 series , need special handling.", level=CommonVariables.WarningLevel)
                    encryption_result_phase = encrypt_inplace_without_seperate_header_file(passphrase_file = passphrase_file, device_item = device_item,disk_util = disk_util, bek_util = bek_util)
                else:
                    encryption_result_phase = encrypt_inplace_with_seperate_header_file(passphrase_file = passphrase_file, device_item = device_item,disk_util = disk_util, bek_util = bek_util)
                
                if(encryption_result_phase == CommonVariables.EncryptionPhaseDone):
                    continue
                else:
                    # do exit to exit from this round
                    return device_item
    return None

def daemon():
    hutil.do_parse_context('Executing')
    try:
        # Ensure the same configuration is executed only once
        # If the previous enable failed, we do not have retry logic here.
        # TODO Remount all
        encryption_marker = EncryptionMarkConfig(logger, encryption_environment)
        if(encryption_marker.config_file_exists()):
            logger.log("encryption is marked.")
        
        """
        search for the bek volume, then mount it:)
        """
        disk_util = DiskUtil(hutil, MyPatching, logger, encryption_environment)

        encryption_config = EncryptionConfig(encryption_environment,logger)
        bek_passphrase_file = None
        """
        try to find the attached bek volume, and use the file to mount the crypted volumes,
        and if the passphrase file is found, then we will re-use it for the future.
        """
        bek_util = BekUtil(disk_util, logger)
        if(encryption_config.config_file_exists()):
            bek_passphrase_file = bek_util.get_bek_passphrase_file(encryption_config)

        if(bek_passphrase_file is None):
            hutil.do_exit(0, 'Enable', CommonVariables.extension_error_status, CommonVariables.passphrase_file_not_found, 'Passphrase file not found.')
        else:
            """
            check whether there's a scheduled encryption task
            """
            logger.log("trying to install the extras")
            MyPatching.install_extras()

            mount_all_result = disk_util.mount_all()

            if(mount_all_result != CommonVariables.process_success):
                logger.log(msg=("mount all failed with code " + str(mount_all_result)), level=CommonVariables.ErrorLevel)
            """
            TODO: resuming the encryption for rebooting suddenly scenario
            we need the special handling is because the half done device can be a error state: say, the file system header missing.so it could be 
            identified.
            """
            ongoing_item_config = OnGoingItemConfig(encryption_environment=encryption_environment, logger=logger)
            if(ongoing_item_config.config_file_exists()):
                header_file_path = ongoing_item_config.get_header_file_path()
                mount_point = ongoing_item_config.get_mount_point()
                if(not none_or_empty(mount_point)):
                    logger.log("mount point is not empty, trying to unmount it first." + str(mount_point))
                    umount_status_code = disk_util.umount(mount_point)
                    logger.log("unmount return code is " + str(umount_status_code))
                if(none_or_empty(header_file_path)):
                    encryption_result_phase = encrypt_inplace_without_seperate_header_file(passphrase_file = bek_passphrase_file, device_item = None,\
                        disk_util = disk_util, bek_util = bek_util, ongoing_item_config = ongoing_item_config)
                else:
                    encryption_result_phase = encrypt_inplace_with_seperate_header_file(passphrase_file = bek_passphrase_file, device_item = None,\
                        disk_util = disk_util, bek_util = bek_util, ongoing_item_config = ongoing_item_config)
                """
                if the resuming failed, we should fail.
                """
                if(encryption_result_phase != CommonVariables.EncryptionPhaseDone):
                    hutil.do_exit(exit_code = 0, operation = 'Enable', status = CommonVariables.extension_error_status, code = CommonVariables.encryption_failed,\
                                  message = 'resuming encryption for ' + str(ongoing_item_config.original_dev_path) + ' failed.')
                else:
                    ongoing_item_config.clear_config()
            else:
                failed_item = None
                if(encryption_marker.get_current_command() == CommonVariables.EnableEncryption):
                    failed_item = enable_encryption_all_in_place(passphrase_file= bek_passphrase_file, encryption_marker = encryption_marker, disk_util = disk_util, bek_util = bek_util)
                elif(encryption_marker.get_current_command() == CommonVariables.EnableEncryptionFormat):
                    failed_item = enable_encryption_format(passphrase = bek_passphrase_file, encryption_marker = encryption_marker, disk_util = disk_util)
                else:
                    logger.log(msg = ("command " + str(encryption_marker.get_current_command()) + " not supported"), level = CommonVariables.ErrorLevel)
                    #TODO do exit here
                if(failed_item != None):
                    hutil.do_exit(exit_code = 0, operation = 'Enable', status = CommonVariables.extension_error_status, code = CommonVariables.encryption_failed,\
                                  message = 'encryption failed for ' + str(failed_item))
                else:
                    hutil.do_exit(exit_code = 0, operation = 'Enable', status = CommonVariables.extension_success_status, code = str(CommonVariables.success), message = encryption_config.get_secret_id())
    except Exception as e:
        # mount the file systems back.
        error_msg = ("Failed to enable the extension with error: %s, stack trace: %s" % (str(e), traceback.format_exc()))
        logger.log(msg = error_msg, level = CommonVariables.ErrorLevel)
        hutil.do_exit(exit_code = 0, operation = 'Enable', status = CommonVariables.extension_error_status, code = str(CommonVariables.encryption_failed), \
                              message = error_msg)

    finally:
        encryption_marker = EncryptionMarkConfig(logger, encryption_environment)
        #TODO not remove it, backed it up.
        logger.log("clearing the encryption mark.")
        encryption_marker.clear_config()
        bek_util.umount_azure_passhprase(encryption_config)
        logger.log("finally in daemon")

def start_daemon():
    args = [os.path.join(os.getcwd(), __file__), "-daemon"]
    logger.log("start_daemon with args:" + str(args))
    #This process will start a new background process by calling
    #    handle.py -daemon
    #to run the script and will exit itself immediatelly.

    #Redirect stdout and stderr to /dev/null.  Otherwise daemon process will
    #throw Broke pipe exeception when parent process exit.
    devnull = open(os.devnull, 'w')
    child = subprocess.Popen(args, stdout=devnull, stderr=devnull)
    
    encryption_config = EncryptionConfig(encryption_environment,logger)
    if(encryption_config.config_file_exists()):
        hutil.do_exit(exit_code = 0, operation = 'Enable', status = CommonVariables.extension_success_status, code = str(CommonVariables.success), message = encryption_config.get_secret_id())
    else:
        hutil.do_exit(exit_code = 0, operation = 'Enable', status = CommonVariables.extension_error_status, code = str(CommonVariables.encryption_failed), message = 'encryption config not found.')


if __name__ == '__main__' :
    main()
