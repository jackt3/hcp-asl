#! /usr/bin/env python3
"""Script to extract PVs from FS binary volumetric segmentation"""

import pathlib
import sys 
import argparse
import multiprocessing as mp 
import os.path as op
import tempfile
import os 

import numpy as np 
import nibabel as nib

import regtricks as rt 
from toblerone.pvestimation import cortex as estimate_cortex

# Labels taken from standard FS LUT, subcortex: 
SUBCORT_LUT = {
    # Left hemisphere
    2 : "WM",
    3 : "GM",
    4 : "CSF",
    5 : "CSF",
    26 : "L_Accu",
    18 : "L_Amyg",
    11 : "L_Caud",
    17 : "L_Hipp",
    13 : "L_Pall",
    12 : "L_Puta",
    9 : "L_Thal",
    10 : "L_Thal",

    24 : "CSF",
    0 : "CSF",
    14 : "CSF", 
    15 : "CSF",
    77 : "WM",
    78 : "WM",
    79 : "WM",

    # Right hemisphere 
    41 : "WM",
    42 : "GM",
    43 : "CSF",
    44 : "CSF",
    58 : "R_Accu",
    54 : "R_Amyg",
    50 : "R_Caud",
    53 : "R_Hipp",
    52 : "R_Pall",
    51 : "R_Puta",
    48 : "R_Thal",
    49 : "R_Thal"
}


# Do not label the following tissues 
IGNORE = [
    6,7,8,45,46,47,     # cerebellum 
    16                  # brstem 
]


# To handle labels from the aparc 
def CTX_LUT(val):
    if val >= 251 and val < 256: 
        return "WM"     # corpus callosum 
    elif val >= 1000 and val < 3000: 
        return "GM"
    elif val >= 3000 and val < 5000:
        return "WM"
    else: 
        return None 

def extract_fs_pvs(aparcseg, surf_dict, t1, asl, superfactor=2, 
                   cores=mp.cpu_count()): 
    """
    Extract and layer PVs according to tissue type, taken from a FS aparc+aseg. 
    Results are stored in ASL-gridded T1 space. 
    Args:
        aparcseg: path to aparc+aseg file
        surf_dict: dict with LWS/LPS/RWS/RPS keys, paths to those surfaces
        t1: path to T1 file
        asl: path to ASL file (for setting resolution)
        superfactor: supersampling factor for intermediate steps
        cores: number CPU cores to use 
    Returns: 
        nibabel Nifti object 
    """

    asl_spc = rt.ImageSpace(asl)
    t1_spc = rt.ImageSpace(t1)
    ref_spc = t1_spc.resize_voxels(asl_spc.vox_size / t1_spc.vox_size)
    high_spc = ref_spc.resize_voxels(1/superfactor, 'ceil')
    aseg_spc = nib.load(aparcseg)
    aseg = aseg_spc.dataobj
    aseg_spc = rt.ImageSpace(aseg_spc)

    # Estimate cortical PVs 
    # FIXME: allow tob to accept imagespace directly here
    with tempfile.TemporaryDirectory() as td:
        ref_path = op.join(td, 'ref.nii.gz')
        ref_spc.touch(ref_path)
        cortex = estimate_cortex(ref=ref_path, struct2ref='I', 
            superfactor=1, cores=cores, **surf_dict)

    # Extract PVs from aparcseg segmentation. Subcortical structures go into 
    # a dict keyed according to their name, whereas general WM/GM are 
    # grouped into the vol_pvs array
    to_stack = {}
    vol_pvs = np.zeros((aseg_spc.size.prod(), 3), dtype=np.float32)
    for label in np.unique(aseg):
        tissue = SUBCORT_LUT.get(label)
        if not tissue: 
            tissue = CTX_LUT(label)
        if tissue: 
            mask = (aseg == label) 
            if tissue == "WM":
                vol_pvs[mask.flatten(),1] = 1
            elif tissue == "GM":
                vol_pvs[mask.flatten(),0] = 1 
            elif tissue == "CSF":
                pass 
            else: 
                to_stack[tissue] = mask.astype(np.float32)
        elif label not in IGNORE: 
            print("Did not assign aseg/aparc label:", label)

    # Super-resolution resampling for the vol_pvs, a la applywarp. 
    # We use an identity transform as we don't actually want to shift the data 
    # 0: GM, 1: WM, 2: CSF, always in the LAST dimension of an array 
    reg = rt.Registration.identity()
    vol_pvs = reg.apply_to_array(vol_pvs.reshape(*aseg.shape, 3), 
                                 aseg_spc, high_spc, order=1, cores=cores)
    vol_pvs = (_sum_array_blocks(vol_pvs, 3 * [superfactor] + [1]) 
               / (superfactor ** 3))
    vol_pvs = vol_pvs.reshape(-1,3)
    vol_pvs[:,2] = np.maximum(0, 1 - vol_pvs[:,:2].sum(1))
    vol_pvs[vol_pvs[:,2] < 1e-2, 2] = 0 
    vol_pvs /= vol_pvs.sum(1)[:,None]
    vol_pvs = vol_pvs.reshape(*ref_spc.size, 3)

    # Stack the subcortical structures into a big 4D volume to resample them
    # all at once, then put them back into a dict 
    keys, values = list(to_stack.keys()), list(to_stack.values())
    del to_stack
    subcorts = reg.apply_to_array(np.stack(values, axis=-1), 
                                  aseg_spc, high_spc, order=1, cores=cores)
    subcorts = (_sum_array_blocks(subcorts, 3 * [superfactor] + [1]) 
                                / (superfactor ** 3))
    subcorts = np.moveaxis(subcorts, 3, 0)
    to_stack = dict(zip(keys, subcorts))

    # Add the cortical and vol PV estimates into the dict, stack them in 
    # a sneaky way (see the stack_images function)
    to_stack['cortex_GM'] = cortex[...,0]
    to_stack['cortex_WM'] = cortex[...,1]
    to_stack['cortex_nonbrain'] = cortex[...,2]
    to_stack['vol_GM'] = vol_pvs[...,0]    
    to_stack['vol_WM'] = vol_pvs[...,1]    
    to_stack['vol_CSF'] = vol_pvs[...,2]
    result = stack_images(to_stack)

    # Squash small values (looks like dodgy output otherwise, but
    # is actually just an artefact of resampling)
    pvmin = result.min()
    if pvmin > 1e-9:
        result[result < 1e-6] = 0 

    return ref_spc.make_nifti(result.reshape((*ref_spc.size, 3))) 


def _sum_array_blocks(array, factor):
    """Sum sub-arrays of a larger array, each of which is sized according to factor. 
    The array is split into smaller subarrays of size given by factor, each of which 
    is summed, and the results returned in a new array, shrunk accordingly. 
    Args:
        array: n-dimensional array of data to sum
        factor: n-length tuple, size of sub-arrays to sum over
    Returns:
        array of size array.shape/factor, each element containing the sum of the 
            corresponding subarray in the input
    """

    if len(factor) != len(array.shape):
        raise RuntimeError("factor must be of same length as number of dimensions")

    if np.any(np.mod(factor, np.ones_like(factor))):
        raise RuntimeError("factor must be of integer values only")

    factor = [ int(f) for f in factor ]

    outshape = [ int(s/f) for (s,f) in zip(array.shape, factor) ]
    out = np.copy(array)

    for dim in range(3):
        newshape = [0] * 4

        for d in range(3):
            if d < dim: 
                newshape[d] = outshape[d]
            elif d == dim: 
                newshape[d+1] = factor[d]
                newshape[d] = outshape[d]
            else: 
                newshape[d+1] = array.shape[d]

        newshape = newshape + list(array.shape[3:])
        out = out.reshape(newshape).sum(dim+1)

    return out 


def stack_images(images):
    """
    Combine the results of estimate_all() into overall PV maps
    for each tissue. Note that the below logic is entirely specific 
    to the surfaces produced by FreeSurfer, FIRST and how they may be
    combined with conventional voulmetric estimates (eg FAST). 
    If you're going off-piste anywhere else then you probably DON'T 
    want to re-use this logic. 

    Args: 
        dictionary of PV maps, keyed as follows: all FIRST subcortical
        structures named by their FIST convention (eg L_Caud); volumetric 
        PV estimates named as vol_CSF/WM/GM; cortex estimates as 
        cortex_GM/WM/non_brain

    Returns: 
        single 4D array of PVs, arranged GM/WM/non-brain in the 4th dim
    """

    # The logic is as follows: 
    # All CSF 
    # Overwrite all cortical PVs onto output
    # Add in volumetric CSF estimates (this recovers ventricles that are
    # missing from cortical estimates)
    # Then within the subcortex only: 
        # All subcortical volume set as WM 
        # Subcortical CSF fixed using vol_CSF
        # Add in subcortical GM for each structure, reducing CSF if required 
        # Set the remainder 1 - (GM+CSF) as WM in voxels that were updated 

    # Pop out vol's estimates  
    csf = images.pop('vol_CSF').flatten()
    wm = images.pop('vol_WM').flatten()
    gm = images.pop('vol_GM')
    shape = (*gm.shape[0:3], 3)

    # Pop the cortex estimates and initialise output as all CSF
    ctxgm = images.pop('cortex_GM').flatten()
    ctxwm = images.pop('cortex_WM').flatten()
    ctxnon = images.pop('cortex_nonbrain').flatten()
    shape = (*ctxgm.shape, 3)
    ctx = np.vstack((ctxgm, ctxwm, ctxnon)).T
    out = np.zeros_like(ctx)
    out[:,2] = 1

    # Then write in cortex estimates from all voxels
    # that contain either WM or GM (on the ctx image)
    mask = np.logical_or(ctx[:,0], ctx[:,1])
    out[mask,:] = ctx[mask,:]

    # Layer in vol's CSF estimates (to get mid-brain and ventricular CSF). 
    # Where vol has suggested a higher CSF estimate than currently exists, 
    # and the voxel does not intersect the cortical ribbon, accept vol's 
    # estimate. Then update the WM estimates, reducing where necessary to allow
    # for the greater CSF volume
    GM_threshold = 0.01 
    ctxmask = (ctx[:,0] > GM_threshold)
    to_update = np.logical_and(csf > out[:,2], ~ctxmask)
    tmpwm = out[to_update,1]
    out[to_update,2] = csf[to_update]
    out[to_update,0] = np.minimum(out[to_update,0], 1 - out[to_update,2])
    out[to_update,1] = np.minimum(tmpwm, 1 - (out[to_update,2] + out[to_update,0]))

    # Sanity checks: total tissue PV in each vox should sum to 1
    # assert np.all(out[to_update,0] <= GM_threshold), 'Some update voxels have GM'
    assert (np.abs(out.sum(1) - 1) < 1e-6).all(), 'Voxel PVs do not sum to 1'
    assert (out > -1e-6).all(), 'Negative PV found'
    assert (out < 1 + 1e-6).all(), 'Large PV found'


    # For each subcortical structure, create a mask of the voxels which it 
    # relates to. The following operations then apply only to those voxels 
    # All subcortical structures interpreted as pure GM 
    # Update CSF to ensure that GM + CSF in those voxels < 1 
    # Finally, set WM as the remainder in those voxels.
    for s in images.values():
        smask = (s.flatten() > 0)
        out[smask,0] = np.minimum(1, out[smask,0] + s.flatten()[smask])
        out[smask,2] = np.minimum(out[smask,2], 1 - out[smask,0])
        out[smask,1] = np.maximum(1 - (out[smask,0] + out[smask,2]), 0)

    # Final sanity check, then rescaling so all voxels sum to unity. 
    out[out < 0] = 0 
    sums = out.sum(1)
    assert (np.abs(out.sum(1) - 1) < 1e-6).all(), 'Voxel PVs do not sum to 1'
    assert (out > -1e-6).all(), 'Negative PV found'
    assert (out < 1 + 1e-6).all(), 'Large PV found'
    out = out / sums[:,None]

    return out.reshape(shape)

if __name__ == "__main__":

    usage = """
    usage: generate_pvs --fsdir <dir> --t1 <path> --asl <path> --out <path> [--stack]
    Extracts PV estimates from FS' binary volumetric segmentation. Saves output 
    in T1 space, with voxels resized to match ASL resolution. 
    
    --aparcseg: path to volumetric FS segmentation (aparc+aseg.mgz)
    --LWS/LPS/RWS/RPS: paths to Left Right White Pial Surfaces
    --t1: path to T1 image for alignment of output
    --asl: path to ASL image
    --out: path to save output at (with suffix _GM, _WM, _CSF, unless --stack)
    --stack: return 4D volume, stacked GM,WM,CSF in last dimension (default false)
    --super: super-sampling level for intermediate resampling (default 2)
    --cores: CPU cores to use (default max)
    --debug: for surface PV estimation (writes ones)
    """

    cmd = ("--LWS testdata/T1w/fsaverage_LR32k/HCA6002236_V1_MR.L.white.32k_fs_LR.surf.gii "
           "--LPS testdata/T1w/fsaverage_LR32k/HCA6002236_V1_MR.L.pial.32k_fs_LR.surf.gii "
           "--RWS testdata/T1w/fsaverage_LR32k/HCA6002236_V1_MR.R.white.32k_fs_LR.surf.gii "
           "--RPS testdata/T1w/fsaverage_LR32k/HCA6002236_V1_MR.R.pial.32k_fs_LR.surf.gii "
           "--t1 testdata/T1w/T1w_acpc_dc_restore_brain.nii.gz --asl testdata/Calib/Calib0/calib0.nii.gz "
           "--aparcseg testdata/T1w/aparc+aseg.nii.gz --out testdata/stacked.nii.gz --stack")

    sys.argv[1:] = cmd.split()

    parser = argparse.ArgumentParser(usage=usage)
    parser.add_argument("--aparcseg", required=True)
    parser.add_argument("--LWS", required=True)
    parser.add_argument("--LPS", required=True)
    parser.add_argument("--RPS", required=True)
    parser.add_argument("--RWS", required=True)
    parser.add_argument("--t1", required=True)
    parser.add_argument("--asl", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--stack", action="store_true")
    parser.add_argument("--super", default=2, type=int)
    parser.add_argument("--cores", default=mp.cpu_count(), type=int)

    args = parser.parse_args(sys.argv[1:])
    surf_dict = dict([ (k, getattr(args, k)) for k in ['LWS', 'LPS', 'RPS', 'RWS'] ])

    pvs = extract_fs_pvs(args.aparcseg, surf_dict, args.t1, 
                         args.asl, args.super, args.cores)

    if args.stack: 
        nib.save(pvs, args.out)

    else: 
        opath = pathlib.Path(args.out)
        spc = rt.ImageSpace(pvs)
        pvs = pvs.dataobj
        for idx,tiss in enumerate(["GM", "WM", "CSF"]):
            n = opath.parent.joinpath(opath.stem.rsplit('.', 1)[0] + f"_{tiss}" + "".join(opath.suffixes))
            spc.save_image(pvs[...,idx], n.as_posix())