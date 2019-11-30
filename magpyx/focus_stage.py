#Imports
from astropy.io import fits
from datetime import datetime, timezone
import glob
from magpyx.utils import ImageStream,indi_send_and_wait
import matplotlib.pyplot as plt
import numpy as np
import os
import purepyindi as indi
import sys
from scipy.optimize import curve_fit

#Construct the Radial Profile Model
def radial_profile(data, center):
    x, y = np.indices((data.shape))
    r = np.sqrt((x - center[0])**2 + (y - center[1])**2)
    r = r.astype(np.int)

    tbin = np.bincount(r.ravel(), data.ravel())
    nr = np.bincount(r.ravel())
    radialprofile = tbin / nr
    return radialprofile 

#Formula for a half gaussian
def gaussian_func(x, a, x0, sigma,c):
    return a * np.exp(-(x-x0)**2/(2*sigma**2)) + c

def gaus(x,a,sigma):
    return a*np.exp(-(x)**2/(2*sigma**2))

#Annotates peak of the Gaussian
def annot_max(xplot,gaussian_func, popt, ax=None):
    xmax = xplot[np.argmax(gaussian_func(xplot, *popt))]
    ymax = gaussian_func(xplot, *popt).max()
    text= "x={:.3f}, y={:.3f}".format(xmax, ymax)
    if not ax:
        ax=plt.gca()
    bbox_props = dict(boxstyle="square,pad=0.3", fc="w", ec="k", lw=0.72)
    arrowprops=dict(arrowstyle="->",connectionstyle="arc")
    kw = dict(xycoords='data',textcoords="axes fraction",
              arrowprops=arrowprops, bbox=bbox_props, ha="right", va="top")
    ax.annotate(text, xy=(xmax, ymax), xytext=(0.8,0.75), **kw)
    
#Annotates full width half max
def annot_fwhm(xplot,gaussian_func, popt, ax=None):
    xfwhm = xplot[gaussian_func(xplot, *popt) < gaussian_func(xplot, *popt).max() / 2].min()
    yfwhm = gaussian_func(xplot, *popt).max()/2
    text= "x={:.3f}, y={:.3f}".format(xfwhm, yfwhm)
    if not ax:
        ax=plt.gca()
    bbox_props = dict(boxstyle="square,pad=0.3", fc="w", ec="k", lw=0.72)
    arrowprops=dict(arrowstyle="->",connectionstyle="arc")
    kw = dict(xycoords='data',textcoords="axes fraction",
              arrowprops=arrowprops, bbox=bbox_props, ha="right", va="top")
    ax.annotate(text, xy=(xfwhm, yfwhm), xytext=(0.8,0.5), **kw)
    
#Fit Image
#Plots the radial profile and the half Gaussian fit of the image
def fit(img, display=False):
    center = np.unravel_index(img.argmax(), img.shape)
    rad_profile = radial_profile(img, center)
    
    x_pos = np.linspace(0, len(rad_profile), len(rad_profile), endpoint=False)
    sigmaguess = x_pos[rad_profile < rad_profile.max() / 2].min()/2.355*2
    initialguess = [rad_profile.max() - rad_profile.min(), 0, sigmaguess, rad_profile.min()]
    xplot = np.linspace(0,75,5000)
        
    try:
        popt, pcov = curve_fit(gaussian_func, x_pos, rad_profile, p0=initialguess)
        peak = gaussian_func(xplot, *popt).max()
        is_good = True
    except RuntimeError:
        is_good = False
        peak = np.max(img)
    
    if display:
        ax = plt.subplot(111)
        ax.plot(rad_profile[0:75], label='Radial Profile')
        if is_good == True:
            ax.plot(xplot, gaussian_func(xplot, *popt),color= 'green',linestyle= 'dashed', label='Gaussian Fit')
        plt.xlabel('Radius (pixels)')
        plt.ylabel('Counts')
        plt.title(f'Gaussian Fit and Radial Profile')
        ax.legend()
                
        annot_max(xplot,gaussian_func, popt)
        annot_fwhm(xplot,gaussian_func, popt)

        plt.show()

    return peak

#Analysis of Peaks
def analysis(all_positions, images, threshold=0.5, display=False):
    all_peaks = []
    for i, img in enumerate(images):
        peak = fit(img)
        all_peaks.append(peak)
    all_peaks = np.asarray(all_peaks)
    max_peak = max(all_peaks)
    goodindices = all_peaks > (threshold*max_peak)
    peaks = all_peaks[goodindices]
    positions = all_positions[goodindices]
    
    z = np.polyfit(positions,peaks,2)
    p = np.poly1d(z)
    min_pos = np.min(positions)
    max_pos = np.max(positions)
    N = 1000
    positions2 = np.linspace(min_pos,max_pos,N)
    y = p(positions2)
    focus_pos_idx = np.argmax(y)
    focus_pos = positions2[focus_pos_idx]
    if display:
        plt.scatter(positions,peaks)
        plt.plot(positions2,y,"r")
        plt.xlabel('Positions (mm)')
        plt.ylabel('Peak Value of Frame')
        plt.title('Peaks')
        #plt.show()
        dateTimeObj = datetime.now(timezone.utc)
        plt.savefig(f'/tmp/Peaks_{dateTimeObj.strftime("%Y-%m-%d-at-%H-%M-%S")}-UTC.png')
        print(f'That maximum peak is {np.max(p(positions2))}')
        print(f'The camera should move to position {focus_pos}')
    return focus_pos

#Main Loop through camera positions
def acquire_data(client, positions, camera='camsci1', stage='stagesci1'):
    camstream = ImageStream(camera)
    images = []
    for i, p in enumerate(positions):
        print(f'Going to {p} mm on {stage}')
        command_stage(client, f'{stage}.position.target', p)
        print('Grabbing images and performing background subtraction')
        raw_img = camstream.grab_latest()
        height = raw_img.shape[0]
        width = raw_img.shape[1]
        slice1 = raw_img[0:3,0:3] #top left
        slice2 = raw_img[0:3,width-3:width] #top right
        slice3 = raw_img[height-3:height,0:3] #bottom left
        slice4 = raw_img[height-3:height,width-3:width] #bottom right
        median = np.median([slice1,slice2,slice3,slice4])
        img = raw_img-median
        images.append(img)
    return images

def command_stage(client, indi_triplet, value):
    command_dict = {indi_triplet : value}
    indi_send_and_wait(client, command_dict, tol=1e-2, wait_for_properties=True, timeout = 30)
    
#ACTUAL FOCUS SCRIPT
def auto_focus_realtime(positions, camera='camsci1', stage='stagesci1', exposure=None, indi_port = 7624):
    client = indi.INDIClient('localhost', indi_port)
    client.start()  #start INDI client
    if exposure is not None:
        command_dict = {f'{camera}.exptime.target' : exposure}
        indi_send_and_wait(client, command_dict, tol=1e-2, wait_for_properties=True, timeout = 30)
    data_cube = acquire_data(client, positions, camera=camera, stage=stage) #capture/bg subtract images
    focus_pos = analysis(positions, data_cube, display=True) #find best focus
    print('The camera is moving to best focus')
    command_stage(client, f'{stage}.position.target', focus_pos) 
    print('The camera is at best focus')

#Console Entry Point
def main():
    import argparse
    parser = argparse.ArgumentParser()

    #parser.add_argument('shmim_name', type=str, help='Name of shared memory name')
    parser.add_argument('-f', '--filepath', type=str, help='File Path')
    parser.add_argument('-c', '--camera', type=str, help='Camera Shared Memory Image')
    parser.add_argument('--start',type=float, default = 0, help='Starting Stage Position')
    parser.add_argument('--stop',type=float, default = 75, help='Ending Stage Position')
    parser.add_argument('--steps',type=int, default = 50, help='Number of Steps')
    parser.add_argument('-exp','--exposure',type=float, default = None, help='Exposure Time')
    args = parser.parse_args()
    if args.filepath is not None and args.camera is not None:
        print('Cannot provide both a file path and a camera')
        sys.exit(1)
    elif args.filepath is None and args.camera is None:
        print('Camera or file path must be provided')
        sys.exit(1)
    elif args.filepath is not None:
        print(args)
        data_cube = fits.getdata(args.filepath)
        positions = np.linspace(args.start,args.stop,args.steps)
        analysis(positions, data_cube, display=True)
    elif args.camera is not None:
        print(args)
        positions = np.linspace(args.start,args.stop,args.steps)
        stage_name = args.camera.replace('cam','stage')
        auto_focus_realtime(positions, camera=args.camera, stage=stage_name, exposure=args.exposure, indi_port = 7624)
