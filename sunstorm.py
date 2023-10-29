#!/usr/bin/env python3
# Sunstorm.py

"""
TODO:
  - `from pyimg4 import IM4P`, use pyimg4 lib instead of calling the bin version
  - Use RemoteZip to speed up having to download the full IPSW
"""

import sys
import os
import argparse
import zipfile
import subprocess
import shutil
import atexit
import tempfile
import glob

# Global variables
ROOT = os.path.dirname(__file__)
DEBUG = 0
LINUX = (sys.platform == 'linux')

# Append PATH
sys.path.append(ROOT + '/src')
os.environ['PATH'] = ((ROOT + '/bin') + ':' + os.environ.get('PATH'))

# Custom util in /src
from manifest import Manifest
import api

program_list = [
  'futurerestore',
  'img4tool',
  'Kernel64Patcher',
  'iBoot64Patcher',
  'ldid',
  'asr64_patcher',
  'restored_external64_patcher',
  # `hfsplus` comes from libdmg-hfsplus
  'hfsplus' if LINUX else 'hdiutil'
]

def print_error(string) -> None:
    # TODO: color support?
    print(f'[!] Error: {string}', file=sys.stderr)

def print_info(string) -> None:
    print(f'[*] Info: {string}')

def cleanup(directory) -> None:
    """ Remove any temp-files created """
    try: 
      if DEBUG:
        return

      shutil.rmtree(directory, ignore_errors=True)
    except:
      print_error(f'Failed to remove {directory} (this is a bug)')

def cleanup_trim(work, suffix) -> None:
    """ Using a glob, remove any files that don't match {suffix} """
    file_list = [fn for fn in glob.glob(f'{work}/**/*', recursive=True) if not os.path.basename(fn).endswith(suffix)]

    for fn in file_list:
      if fn.startswith(work) and not DEBUG: # -> Don't remove user files, verify
        try:
          os.remove(fn)
        except:
          pass

def check_for_command(prog) -> bool:
    """ Use the `command` shell-builtin to test for program """
    return bool(shutil.which(prog))

def check_for_dependencies() -> None:
    """
    Loop over {program_list}, check for every command.

    Exits on error
    """
    for prog in program_list:
      if not check_for_command(prog):
        print_error(f'"{prog}" not found, please install it.')
        sys.exit(1)

def execute(arguments, ignore_errors = False) -> str:
  """
  Wrapper for `subprocess.run`,
    - Stops process on error.
    - Returns stdout on success

  FIXME: There should be a better way to wrap & check status code on each command
  """
  if DEBUG:
    print(arguments)

  result = subprocess.run(arguments, capture_output=True)
  if (result.returncode > 0):
    print_error(f'Command "{arguments}" returned non-zero')
    if not ignore_errors:
      sys.exit(1)

  return result.stdout

def linux_hfsplus_sync(work) -> None:
    """
      `rsync`-like function to add files to ramdisk

      We don't have the ramdisk mounted so we have to manually make dirs,
        symlinks, files, and then finally chmod to same/correct permissions
    """

    # XXX: There is a `hfsplus addall` command, but we want to ensure correct permissions and symlinks:
    # hfsplus might error out with addall before it finishes
    # execute(['hfsplus', f'{work}/ramdisk.dmg', 'addall', f'{work}/ramdisk'], ignore_errors=True)
    if not (os.path.exists(f'{work}/ramdisk') and os.path.exists(f'{work}/ramdisk.dmg')):
      print_error(f'Missing {work}/ramdisk or {work}/ramdisk.dmg (this is a bug)')
      sys.exit(1)

    # make directories
    for directory in glob.iglob(f'{work}/ramdisk/*/**/', recursive=True):
      path = os.path.relpath(directory, f'{work}/ramdisk')

      if not path.startswith('/'):
        path = '/' + path

      execute(['hfsplus', f'{work}/ramdisk.dmg', 'mkdir', path], ignore_errors=True)

    # copy files
    for file in glob.iglob(f'{work}/ramdisk/**', recursive=True):
      stat = os.stat(file, follow_symlinks=False)
      permission = oct(stat.st_mode)[-3:]
      path = os.path.relpath(file, f'{work}/ramdisk')
      dirname = os.path.dirname(path)

      if not path.startswith('/'):
        path = '/' + path

      if (os.path.isdir(file)):
        continue

      # ensure correct permissions for binaries; `permissions less than` to ensure setuid binaries keep permissions
      if (dirname.endswith(('bin', 'sbin', 'libexec')) and int(permission) < 755):
        permission = "100755"

      if (os.path.islink(file)):
        symlink = os.readlink(file)

        execute(['hfsplus', f'{work}/ramdisk.dmg', 'link', path, symlink], ignore_errors=True)
      else:
        # Assume regular file:
        execute(['hfsplus', f'{work}/ramdisk.dmg', 'add', file, path], ignore_errors=True)
        
      execute(['hfsplus', f'{work}/ramdisk.dmg', 'chmod', permission, path], ignore_errors=True)

def prep_restore(ipsw, blob, boardconfig, kpp, legacy, skip_baseband, extra_ramdisk):
    # tempdir
    work = tempfile.mkdtemp(prefix='restore-')
    # make a directory in the work directory called ramdisk
    os.mkdir(f'{work}/ramdisk')
    # register cleanup trap
    atexit.register(cleanup, work)

    # extract the IPSW to the work directory
    print_info('Extracting IPSW')
    with zipfile.ZipFile(ipsw, 'r') as z:
        z.extractall(work)
    
    # read manifest from {work}/BuildManifest.plist
    with open(f'{work}/BuildManifest.plist', 'rb') as f:
        manifest = Manifest(f.read())
    # get the ramdisk name
    ramdisk_path = manifest.get_comp(boardconfig, 'RestoreRamDisk')
    if ramdisk_path == None:
        print_error("Error: BoardConfig was not recognized")
        sys.exit(1)

    # extract it using img4
    print_info('Extracting RamDisk')
    execute(['img4', '-i', f'{work}/{ramdisk_path}', '-o', f'{work}/ramdisk.dmg'])

    print_info('Mounting RamDisk')
    if LINUX:
      # extract files needed using hfsplus
      os.makedirs(f'{work}/ramdisk/usr/sbin/')
      os.makedirs(f'{work}/ramdisk/usr/local/bin/')
      
      execute(['hfsplus', f'{work}/ramdisk.dmg', 'extract', '/usr/sbin/asr', f'{work}/ramdisk/usr/sbin/asr'])
      execute(['hfsplus', f'{work}/ramdisk.dmg', 'extract', '/usr/local/bin/restored_external', f'{work}/ramdisk/usr/local/bin/restored_external'])

      execute(['hfsplus', f'{work}/ramdisk.dmg', 'rm', '/usr/sbin/asr'])
      execute(['hfsplus', f'{work}/ramdisk.dmg', 'rm', '/usr/local/bin/restored_external'])
    else:
      # mount it using hdiutil
      print_info('Mounting RamDisk')
      execute(['hdiutil', 'attach', f'{work}/ramdisk.dmg', '-mountpoint', f'{work}/ramdisk'])

    if extra_ramdisk:
      print_info('Extracting custom ramdisk tar-ball')
      execute(['tar', '-C', f'{work}/ramdisk/', '-xf', f'{extra_ramdisk}']) # FIXME: Would this work on macos before growing?
      # grow the ramdisk to .5GB
      if LINUX:
        execute(['hfsplus', f'{work}/ramdisk.dmg', 'grow', str(round(5e+8))])
      else:
        execute(['hdiutil', 'resize', '-size', '5120MB', f'{work}/ramdisk.dmg'])

    # patch asr into the ramdisk
    print_info('Patching ASR in the RamDisk')
    execute(['asr64_patcher', f'{work}/ramdisk/usr/sbin/asr', f'{work}/patched_asr'])

    # extract the ents and save it to {work}/asr_ents.plist like:     execute(['ldid', '-e', '{work}/ramdisk/usr/sbin/asr', '>', '{work}/asr.plist'])
    print_info('Extracting ASR Ents')
    with open(f'{work}/asr.plist', 'wb') as f:
        f.write(execute(['ldid', '-e', f'{work}/ramdisk/usr/sbin/asr']))
        f.flush()
    # resign it using ldid
    print_info('Resigning ASR')
    execute(['ldid', f'-S{work}/asr.plist', f'{work}/patched_asr'])
    # chmod 755 the new asr
    print_info('Chmoding ASR')
    execute(['chmod', '-R', '755', f'{work}/patched_asr'])
    # copy the patched asr back to the ramdisk
    print_info('Copying Patched ASR back to the RamDisk')
    execute(['cp', f'{work}/patched_asr', f'{work}/ramdisk/usr/sbin/asr'])

    if legacy:
        print_info('Legacy mode, skipping restored_external')
    else:
        # patch restored_external 
        print_info('Patching Restored External')
        execute(['restored_external64_patcher', f'{work}/ramdisk/usr/local/bin/restored_external', f'{work}/restored_external_patched'])
        #resign it using ldid
        print_info('Extracting Restored External Ents')
        with open(f'{work}/restored_external.plist', 'wb') as f:
            f.write(execute(['ldid', '-e', f'{work}/ramdisk/usr/local/bin/restored_external']))
            f.flush()
        # resign it using ldid
        print_info('Resigning Restored External')
        execute(['ldid', f'-S{work}/restored_external.plist', f'{work}/restored_external_patched'])
        # chmod 755 the new restored_external
        print_info('Chmoding Restored External')
        execute(['chmod', '-R', '755', f'{work}/restored_external_patched'])
        # copy the patched restored_external back to the ramdisk
        print_info('Copying Patched Restored External back to the RamDisk')
        execute(['cp', f'{work}/restored_external_patched', f'{work}/ramdisk/usr/local/bin/restored_external'])

    # detach the ramdisk
    print_info('Detaching RamDisk')
    if LINUX:
      if extra_ramdisk:
        linux_hfsplus_sync(work)

      # Ensure `asr` and `restored_external` make it with correct permissions
      execute(['hfsplus', f'{work}/ramdisk.dmg', 'add', f'{work}/ramdisk/usr/sbin/asr', '/usr/sbin/asr'])
      execute(['hfsplus', f'{work}/ramdisk.dmg', 'add', f'{work}/ramdisk/usr/local/bin/restored_external', '/usr/local/bin/restored_external'])

      execute(['hfsplus', f'{work}/ramdisk.dmg', 'chmod', '100755', '/usr/sbin/asr'])
      execute(['hfsplus', f'{work}/ramdisk.dmg', 'chmod', '100755', '/usr/local/bin/restored_external'])
    else: 
      execute(['hdiutil', 'detach', f'{work}/ramdisk'])

    # create the ramdisk using pyimg4
    print_info('Creating RamDisk')
    execute([sys.executable, '-m', 'pyimg4', 'im4p', 'create', '-i', f'{work}/ramdisk.dmg', '-o', f'{work}/ramdisk.im4p', '-f', 'rdsk'])
    # get kernelcache name from manifest
    kernelcache = manifest.get_comp(boardconfig, 'RestoreKernelCache')
    # extract the kernel using pyimg4 like this: pyimg4 im4p extract -i kernelcache -o kcache.raw --extra kpp.bin 
    print_info('Extracting Kernel')

    extract_kernel_args = [sys.executable, '-m', 'pyimg4', 'im4p', 'extract', '-i', f'{work}/' + kernelcache, '-o', f'{work}/kcache.raw']

    if kpp:
        extract_kernel_args += ['--extra', f'{work}/kpp.bin']

    execute(extract_kernel_args)
    # patch the kernel using kernel64patcher like this: Kernel64Patcher kcache.raw krnl.patched -f -a
    print_info('Patching Kernel')
    execute(['Kernel64Patcher', f'{work}/kcache.raw', f'{work}/krnl.patched', '-f', '-a'])
    # rebuild the kernel like this: pyimg4 im4p create -i krnl.patched -o krnl.im4p --extra kpp.bin -f rkrn --lzss (leave out --extra kpp.bin if you dont have kpp)
    print_info('Rebuilding Kernel')

    rebuild_kernel_args = [sys.executable, '-m', 'pyimg4', 'im4p', 'create', '-i', f'{work}/krnl.patched', '-o', f'{work}/krnl.im4p', '-f', 'rkrn', '--lzss']

    if kpp:
      rebuild_kernel_args += ['--extra', f'{work}/kpp.bin']

    execute(rebuild_kernel_args)

    cleanup_trim(work, 'im4p')

    print_info('Moving files')
    # Done, move files to root (dirname $0)
    shutil.move(work, ROOT)
    work = os.path.realpath(f'{ROOT}/{os.path.basename(work)}')
    print_info(f'Done! Files moved to "{work}"')

    futurerestore_args = ['futurerestore', '-t', blob, '--use-pwndfu', '--skip-blob', '--rdsk', f'{work}/ramdisk.im4p', '--rkrn', f'{work}/krnl.im4p', '--latest-sep', '--no-baseband' if skip_baseband else '--latest-baseband', ipsw]
    futurerestore_args_string = ' '.join(futurerestore_args)

    print_info('You can restore the device anytime by running the following command with the device in a pwndfu state:')
    print(futurerestore_args_string)

    # write to a file to help remember
    with open(f'{work}/restore.command', 'wt') as f:
      f.write(futurerestore_args_string)

    # Ask user if they would like to restore the device
    ask = input('Would you like to restore now? [Yy/Nn]: ')
    if ask == 'y' or ask == 'Y':
      execute(futurerestore_args)

    # Remove the trap so it doesn't error out
    atexit.unregister(cleanup)

    return

def prep_boot(ipsw, blob, boardconfig, kpp, identifier, legacy, extra_ramdisk, boot_arguments):
    # tempdir
    work = tempfile.mkdtemp(prefix='boot-')
    # make a directory in the work directory called ramdisk
    os.mkdir(f'{work}/ramdisk')
    # register cleanup trap
    atexit.register(cleanup, work)

    # unzip the ipsw
    print_info('Unzipping IPSW')
    with zipfile.ZipFile(ipsw, 'r') as z:
        z.extractall(work)

    with open(f'{work}/BuildManifest.plist', 'rb') as f:
        manifest = Manifest(f.read())

    # get ProductBuildVersion from manifest
    print_info('Getting ProductBuildVersion')
    productbuildversion = manifest.getProductBuildVersion()
    ibss_iv, ibss_key, ibec_iv, ibec_key = api.get_keys(identifier, boardconfig, productbuildversion)

    if not (ibss_iv and ibss_key and ibec_iv and ibec_key):
      print_error('Possible incorrect identifier or boardconfig')

    # get ibec and ibss from manifest
    print_info('Getting IBSS and IBEC')
    ibss = manifest.get_comp(boardconfig, 'iBSS')
    ibec = manifest.get_comp(boardconfig, 'iBEC')

    # decrypt ibss like this:  img4 -i ibss -o ibss.dmg -k ivkey
    print_info('Decrypting IBSS')
    execute(['img4', '-i', f'{work}/' + ibss, '-o', f'{work}/ibss.dmg', '-k', ibss_iv + ibss_key])

    # decrypt ibec like this:  img4 -i ibec -o ibec.dmg -k ivkey
    print_info('Decrypting IBEC')
    execute(['img4', '-i', f'{work}/' + ibec, '-o', f'{work}/ibec.dmg', '-k', ibec_iv + ibec_key])

    # patch ibss like this:  iBoot64Patcher ibss.dmg ibss.patched
    print_info('Patching IBSS')
    execute(['iBoot64Patcher', f'{work}/ibss.dmg', f'{work}/ibss.patched'])

    # patch ibec like this:  iBoot64Patcher ibec.dmg ibec.patched -b "-v"
    print_info('Patching IBEC')
    execute(['iBoot64Patcher', f'{work}/ibec.dmg', f'{work}/ibec.patched', '-n', '-b', f'-v {boot_arguments}'])

    # convert blob into im4m like this: img4tool -e -s blob -m IM4M
    print_info('Converting BLOB to IM4M')
    execute(['img4tool', '-e', '-s', blob, '-m', 'IM4M'])

    # convert ibss into img4 like this:  img4 -i ibss.patched -o ibss.img4 -M IM4M -A -T ibss
    print_info('Converting IBSS to IMG4')
    execute(['img4', '-i', f'{work}/ibss.patched', '-o', f'{work}/ibss.img4', '-M', 'IM4M', '-A', '-T', 'ibss'])

    # convert ibec into img4 like this:  img4 -i ibec.patched -o ibec.img4 -M IM4M -A -T ibec
    print_info('Converting IBEC to IMG4')
    execute(['img4', '-i', f'{work}/ibec.patched', '-o', f'{work}/ibec.img4', '-M', 'IM4M', '-A', '-T', 'ibec'])

    # get the names of the devicetree and trustcache
    print_info('Getting Device Tree and TrustCache')
    # read manifest from {work}/BuildManifest.plist
    trustcache = manifest.get_comp(boardconfig, 'StaticTrustCache') if not legacy else None
    devicetree = manifest.get_comp(boardconfig, 'DeviceTree')

    # sign them like this  img4 -i devicetree -o devicetree.img4 -M IM4M -T rdtr
    print_info('Signing Device Tree')
    execute(['img4', '-i', f'{work}/' + devicetree, '-o', f'{work}/devicetree.img4', '-M', 'IM4M', '-T', 'rdtr'])

    # sign them like this   img4 -i trustcache -o trustcache.img4 -M IM4M -T rtsc
    if not legacy:
        print_info('Signing Trust Cache')
        execute(['img4', '-i', f'{work}/' + trustcache, '-o', f'{work}/trustcache.img4', '-M', 'IM4M', '-T', 'rtsc'])

    # grab kernelcache from manifest
    print_info('Getting Kernel Cache')
    kernelcache = manifest.get_comp(boardconfig, 'KernelCache')

    # extract the kernel like this:  pyimg4 im4p extract -i kernelcache -o kcache.raw --extra kpp.bin 
    print_info('Extracting Kernel')
    extract_kernel_args = [sys.executable, '-m', 'pyimg4', 'im4p', 'extract', '-i', f'{work}/' + kernelcache, '-o', f'{work}/kcache.raw']

    if kpp:
        extract_kernel_args += ['--extra', f'{work}/kpp.bin']

    execute(extract_kernel_args)

    # patch it like this:   Kernel64Patcher kcache.raw krnlboot.patched -f
    print_info('Patching Kernel')
    execute(['Kernel64Patcher', f'{work}/kcache.raw', f'{work}/krnlboot.patched', '-f', '-a' if extra_ramdisk else ''])
    # -> Taurine will refuse to jailbreak if `-a` is applied / AFMI is disabled

    # convert it like this:   pyimg4 im4p create -i krnlboot.patched -o krnlboot.im4p --extra kpp.bin -f rkrn --lzss
    print_info('Converting Kernel')
    convert_kernel_args = [sys.executable, '-m', 'pyimg4', 'im4p', 'create', '-i', f'{work}/krnlboot.patched', '-o', f'{work}/krnlboot.im4p', '-f', 'rkrn', '--lzss']

    if kpp:
      convert_kernel_args += ['--extra', f'{work}/kpp.bin']
    
    execute(convert_kernel_args)

    # sign it like this:  pyimg4 img4 create -p krnlboot.im4p -o krnlboot.img4 -m IM4M
    print_info('Signing Kernel')
    execute([sys.executable, '-m', 'pyimg4', 'img4', 'create', '-p', f'{work}/krnlboot.im4p', '-o', f'{work}/krnlboot.img4', '-m', 'IM4M'])

    if extra_ramdisk:
      # TODO: Inforce DRY here
      # extract it using img4
      ramdisk_path = manifest.get_comp(boardconfig, 'RestoreRamDisk')
      if ramdisk_path == None:
          print_error("Error: BoardConfig was not recognized")
          sys.exit(1)

      print_info('Extracting RamDisk')
      execute(['img4', '-i', f'{work}/{ramdisk_path}', '-o', f'{work}/ramdisk.dmg'])

      print_info('Extracting custom ramdisk tar-ball')
      execute(['tar', '-C', f'{work}/ramdisk/', '-xf', f'{extra_ramdisk}'])
      # grow the ramdisk to .5GB
      if LINUX:
        execute(['hfsplus', f'{work}/ramdisk.dmg', 'grow', str(round(5e+8))])
      else:
        execute(['hdiutil', 'resize', '-size', '5120MB', f'{work}/ramdisk.dmg'])

      # detach the ramdisk
      print_info('Detaching RamDisk')
      if LINUX:
        linux_hfsplus_sync(work)
      else: 
        execute(['hdiutil', 'detach', f'{work}/ramdisk'])

      print_info('Creating RamDisk')
      execute(['img4', '-i', f'{work}/ramdisk.dmg', '-o', f'{work}/ramdisk.img4', '-M', 'IM4M', '-A', '-T', 'rdsk'])

    cleanup_trim(work, 'img4')

    print_info('Moving files')
    shutil.move(work, ROOT)
    work = os.path.realpath(f'{ROOT}/{os.path.basename(work)}')
    print_info(f'Done! Files moved to "{work}"')

    # done
    print_info('You can boot the restored device anytime by running the following command with the device in a pwndfu state:')
    print(f'{os.path.realpath(ROOT) + "/scripts/"}' + ('boot.sh' if kpp else 'boot-A10plus.sh'))
    print_info(f'Make sure to `cd` into "{work}" before running!')

    # Remove the trap so it doesn't error out
    atexit.unregister(cleanup)

    return

def main():
    # Arg-parser:
    credit = """
    sunst0rm:
    Made by mineek, some code by m1n1exploit
    """

    parser = argparse.ArgumentParser(description='iOS Tethered IPSW Restore', epilog=credit)
    conflict = parser.add_mutually_exclusive_group(required=True)

    parser.add_argument('-i', '--ipsw', help='IPSW to restore', required=True)
    parser.add_argument('-t', '--blob', help='Blob (shsh2) to use', required=True)
    parser.add_argument('-d', '--boardconfig', help='BoardConfig to use', required=True)
    parser.add_argument('-kpp', '--kpp', help='Use Kernel Patch Protection (KPP) (Required on devices lower than A9)', required=False, action='store_true')
    parser.add_argument('-id', '--identifier', help='Identifier to use (ex. iPhoneX,X)', required=False)
    parser.add_argument('--legacy', help='Use Legacy Mode (iOS 11 or lower)', required=False, action='store_true')
    parser.add_argument('--skip-baseband', help='Skip Cellular Baseband', required=False, action='store_true')
    parser.add_argument('--extra-ramdisk', help='Add extra files to the ramdisk (must be $file.tar.gz that extracts without parent directory)', required=False)
    parser.add_argument('--boot-arguments', help='Add extra boot arguments when creating boot files', required=False)
    # These options cannot be used together:
    conflict.add_argument('-b', '--boot', help='Create Boot files', action='store_true')
    conflict.add_argument('-r', '--restore', help='Create Restore files', action='store_true')
    # Finally, parse:
    args = parser.parse_args()
    # Arg-parser will exit for us if there's a argument error
    check_for_dependencies()

    # Cast/modify arguments here before passing
    restore = bool(args.restore)
    boot = bool(args.boot)
    ipsw  = os.path.realpath(args.ipsw)
    blob = os.path.realpath(args.blob)
    boardconfig = str(args.boardconfig).lower() # lowercase board to avoid missing errors
    kpp = bool(args.kpp)
    legacy = bool(args.legacy)
    skip_baseband = bool(args.skip_baseband)
    extra_ramdisk = os.path.realpath(args.extra_ramdisk) if args.extra_ramdisk else None
    boot_arguments = str(args.boot_arguments) if args.boot_arguments else ''
    identifier = args.identifier

    if not os.path.exists(ipsw):
      print_error(f'IPSW "{ipsw}" doesn\'t exist')
      sys.exit(1)

    if not os.path.exists(blob):
      print_error(f'Blob "{blob}" doesn\'t exist')
      sys.exit(1)

    if boot and not identifier:
      print_error('You need to specify an identifier (--identifier)')
      sys.exit(1)

    if extra_ramdisk and (not os.path.exists(extra_ramdisk) or not (extra_ramdisk.endswith('.tar.gz'))):
      print_error('Extra ramdisk must be in the $file.tar.gz format')
      sys.exit(1)

    if restore:
      prep_restore(ipsw, blob, boardconfig, kpp, legacy, skip_baseband, extra_ramdisk)
    elif boot:
      prep_boot(ipsw, blob, boardconfig, kpp, identifier, legacy, extra_ramdisk, boot_arguments)
    else:
      print_error('No mode selected (this is a bug)')
      print(args)
      sys.exit(1)

    sys.exit(0)

if __name__ == '__main__':
  main()
