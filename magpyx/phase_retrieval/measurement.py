import numpy as np
from time import sleep

from poppy.zernike import arbitrary_basis
from astropy.io import fits

from purepyindi import INDIClient

from ..imutils import register_images, slice_to_valid_shape, center_of_mass
from ..utils import ImageStream, indi_send_and_wait
from ..instrument import move_stage, take_dark

import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('fdpr')

def take_measurements_from_config(config_params, dm_cmds=None, client=None, dmstream=None, dmdivstream=None, camstream=None, darkim=None,  restore_dm=True):
    
    skip_indi = config_params.get_param('diversity', 'skip_indi', bool)

    if (client is None) and (not skip_indi):
        # open indi client connection
        client = INDIClient('localhost', config_params.get_param('diversity', 'port', int))
        client.start()

    if dmstream is None:
        # open shmims
        dmstream = ImageStream(config_params.get_param('diversity', 'dmchannel', str))
    if dmdivstream is None:
        dmdivstream = ImageStream(config_params.get_param('diversity', 'dmdivchannel', str))
    if camstream is None:
        camname = config_params.get_param('camera', 'name', str)
        camstream = ImageStream(camname)

    if darkim is None and (not skip_indi):
        # take a dark (eventually replace this with the INDI dark [needs some kind of check to see if we have a dark, I guess])
        darkim = take_dark(camstream, client, camname, config_params.get_param('diversity', 'ndark', int))
    else:
        darkim = None

    dmdelay = config_params.get_param('diversity', 'dmdelay', float)
    indidelay = config_params.get_param('diversity', 'indidelay', float)

    # measure
    div_type = config_params.get_param('diversity', 'type', str)
    if div_type.lower() == 'dm':
        # get defocus mode
        with fits.open(config_params.get_param('interaction', 'dm_mask', str)) as f:
            dm_mask = f[0].data
        zbasis = arbitrary_basis(dm_mask, nterms=4, outside=0)
        defocus_mode = zbasis[-1]
        imcube = measure_dm_diversity(camstream,
                                      dmstream,
                                      dmdivstream,
                                      defocus_mode,
                                      config_params.get_param('diversity', 'values', float),
                                      config_params.get_param('diversity', 'navg', float),
                                      darkim=darkim,
                                      dm_cmds=dm_cmds,
                                      dmdelay=dmdelay,
                                      indidelay=indidelay,
                                      restore_dm=restore_dm
                                      )
    else: # stage diversity
        positions = np.asarray(config_params.get_param('diversity', 'values', float)) + config_params.get_param('diversity', 'stage_focus', float)
        imcube = measure_stage_diversity(client,
                                camstream,
                                dmstream,
                                config_params.get_param('diversity', 'camstage', str),
                                positions,
                                config_params.get_param('diversity', 'navg', float),
                                darkim=darkim,
                                dm_cmds=dm_cmds,
                                dmdelay=dmdelay,
                                final_position=positions[0],
                                restore_dm=restore_dm
                                )
    print(imcube.shape)
    # clip if needed
    shape = imcube.shape
    naxes = len(shape)
    N = config_params.get_param('estimation', 'N', int)
    if N < shape[-1]: # assume the camera image is square
        logger.info(f'Expected shape {N}x{N} but got shape {shape[-2:]}. Clipping to {N}x{N} about center of mass.')
        imcube_reduced = []
        for im in imcube:
            if naxes == 4:
                # in this case, im is actually a cube
                # so define a slice around the mean center of mass
                # (e.g., response matrix measurements)
                im0 = np.mean(im,axis=0)
                com = center_of_mass(im0)
                totalslice = (slice(None),) + slice_to_valid_shape(im0, com, N, return_slice=True)
                imcube_reduced.append(im[totalslice])
            elif naxes == 3:
                # here, im is actually an image
                # (e.g., measurements for a single estimate)
                com = center_of_mass(im)
                newim = slice_to_valid_shape(im, com, N)
                imcube_reduced.append(newim)
        imcube = np.asarray(imcube_reduced)
    if N > shape[-1]: # assume the camera image is square
        logger.warning(f'Camera frames are smaller than expected. Expected {N}x{N} but got {shape[-2:]}.')

    return imcube

def measure_dm_diversity(camstream, dmstream, dmdivstream, defocus_mode, defocus_vals, nimages, dm_cmds=None, restore_dm=True, dmdelay=None, indidelay=None, improc='mean', darkim=None):

    # get the initial defocus set on the DM
    #client.wait_for_properties([f'{device}.current_amps',])
    #defocus0 = client[f'{device}.current_amps.0002']
    divcmd = dmdivstream.grab_latest()
    
    # commanding DM
    dm_shape = dmstream.grab_latest().shape
    dm_type = dmstream.buffer.dtype
    # keep track of channel cmd
    if restore_dm:
        curcmd = dmstream.grab_latest()
    else:
        curmd = np.zeros(dm_shape)

    if darkim is None:
        darkim = 0

    allims = []
    for j, curdefocus in enumerate(defocus_vals):
        print(f'Moving to focus position {j+1}')
                                
        # send INDI command to apply defocus to DM
        #client[f'{device}.current_amps.0002'] = defocus0 + curdefocus
        dmdivstream.write(defocus_mode*curdefocus + divcmd)
        if indidelay is not None:
            sleep(indidelay)

        # loop over DM commands, and take measurements
        curims = []
        if dm_cmds is None:
            dm_cmds = [np.zeros(dm_shape, dtype=dm_type) + curcmd,]
        for cmd in dm_cmds:
            dmstream.write(cmd.astype(dm_type))
            cnt0 = camstream.md.cnt0 # grab the current camera frame number
            if dmdelay is not None:
                #sleep(dmdelay)
                newcnt0 = cnt0 + dmdelay # expected camera frame number for this DM command
            else:
                newcnt0 = None # don't wait otherwise
            imlist = np.asarray(camstream.grab_many(nimages, cnt0_min=newcnt0))
            if improc == 'register':
                im = np.mean(register_images(imlist - darkim, upsample=10), axis=0)
            else:
                im = np.mean(imlist, axis=0) - darkim
            curims.append(im)
        allims.append(curims)     
        
    # set defocus back to the starting point
    if restore_dm:
        dmstream.write(curcmd)
    #client[f'{device}.current_amps.0002'] = defocus0
    dmdivstream.write(divcmd)
    if indidelay is not None:
        sleep(indidelay)
    return np.squeeze(allims)

def measure_stage_diversity(client, camstream, dmstream, camstage, defocus_positions, nimages, final_position=None, dm_cmds=None, restore_dm=True, dmdelay=None, improc='mean', darkim=None):
    dm_shape = dmstream.grab_latest().shape
    dm_type = dmstream.buffer.dtype
    
    # keep track of channel cmd
    if restore_dm:
        curcmd = dmstream.grab_latest()
    else:
        curcmd = np.zeros(dm_shape)

    if darkim is None:
        darkim = 0

    allims = []
    for j, pos in enumerate(defocus_positions):
        print(f'Moving to focus position {j+1}')
                                
        # block until stage is in position
        move_stage(client, camstage, pos, block=True)
        sleep(0.1)

        # loop over DM commands, and take measurements
        curims = []
        if dm_cmds is None:
            dm_cmds = [np.zeros(dm_shape, dtype=dm_type) + curcmd,]
        for cmd in dm_cmds:
            dmstream.write(cmd.astype(dm_type))
            dmstream.write(cmd.astype(dm_type)) # for good measure
            cnt0 = camstream.md.cnt0 # grab the current camera frame number
            if dmdelay is not None:
                #sleep(dmdelay)
                newcnt0 = cnt0 + dmdelay # expected camera frame number for this DM command
            else:
                newcnt0 = None # don't wait otherwise
            imlist = np.asarray(camstream.grab_many(nimages, cnt0_min=newcnt0))
            if improc == 'register':
                im = np.mean(register_images(imlist - darkim, upsample=10), axis=0)
            else:
                im = np.mean(imlist, axis=0) - darkim
            curims.append(im)
        allims.append(curims)  
        
        if restore_dm:
            dmstream.write(curcmd.astype(dm_type)) # reset between stage moves (minimize creep on ALPAOs)  
        
    # restore
    if restore_dm:
        dmstream.write(curcmd)
    if final_position is not None:
        move_stage(client, camstage, final_position, block=False)
    return np.squeeze(allims)