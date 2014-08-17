'''
Created on Aug 5, 2014

@author: zwicker

Note that the OpenCV convention is to store images in [row, column] format
Thus, a point in an image is referred to as image[coord_y, coord_x]
However, a single point is stored as point = (coord_x, coord_y)
Similarly, we store rectangles as (coord_x, coord_y, width, height)

Generally, x-values increase from left to right, while y-values increase from
top to bottom. The origin is thus in the upper left corner.
'''

from __future__ import division

import datetime
import logging
import os

import numpy as np
import scipy.ndimage as ndimage
from scipy.optimize import leastsq
from scipy.spatial import distance
import cv2

from video.io import VideoFileStack, ImageShow
from video.filters import FilterBlur, FilterCrop, FilterMonochrome
from video.analysis.regions import (get_largest_region, find_bounding_box,
                                    rect_to_slices, corners_to_rect)
from video.analysis.curves import curve_length, make_curve_equidistant, simplify_curve
from video.utils import display_progress
from video.composer import VideoComposerListener, VideoComposer


from .data_handler import DataHandler, ObjectTrajectory, Object, SandProfile
from .burrow_finder import BurrowFinder

import debug



class FirstPass(DataHandler):
    """
    analyzes mouse movies
    """
    
    video_filename_pattern = 'raw_video/*'
    video_output_codec = 'libx264'
    video_output_extension = '.mov'
    video_output_bitrate = '2000k'
    
    def __init__(self, folder, frames=None, crop=None, prefix='',
                 tracking_parameters=None, debug_output=None):
        """ initializes the whole mouse tracking and prepares the video filters """
        
        # initialize the dictionary holding result information
        super(FirstPass, self).__init__(folder, prefix, tracking_parameters)
        
        self.log_event('init_begin', 'Start initializing the video analysis.')
        
        # initialize video
        self.video = VideoFileStack(os.path.join(folder, self.video_filename_pattern))
        self.data['video_raw'] = {'filename_pattern': self.video_filename_pattern,
                                  'frame_count': self.video.frame_count,
                                  'size': '%d x %d' % self.video.size,
                                  'fps': self.video.fps}
        
        # restrict the analysis to an interval of frames
        if frames is not None:
            self.video = self.video[frames[0]:frames[1]]
        else:
            frames = (0, self.video.frame_count) 
        
        self.debug_output = [] if debug_output is None else debug_output
        self.params = self.data['tracking_parameters']
        
        # setup internal structures that will be filled by analyzing the video
        self.burrow_finder = BurrowFinder(self.params)
        self._cache = {}           # cache that some functions might want to use
        self.debug = {}            # dictionary holding debug information
        self.sand_profile = None   # current model of the sand profile
        self.mice = []             # list of plausible mouse models in current frame
        self._mouse_pos_estimate = []  # list of estimated mouse positions
        self.explored_area = None # region the mouse has explored yet
        self.frame_id = None       # id of the current frame
        self.data['mouse.has_moved'] = False

        # restrict the video to the region of interest (the cage)
        cropping_rect = self.crop_video_to_cage(crop)
        self.data['video_analyzed'] = {'frame_slice': '%d to %d' % frames,
                                       'frame_count': self.video.frame_count,
                                       'region_specified': crop,
                                       'region_cage': cropping_rect,
                                       'size': '%d x %d' % self.video.size,
                                       'fps': self.video.fps}

        # blur the video to reduce noise effects    
        self.video_blurred = FilterBlur(self.video, self.params['video.blur_radius'])
        first_frame = self.video_blurred[0]
        
        # initialize the background model
        self._background = np.array(first_frame, dtype=float)

        # estimate colors of sand and sky
        self.find_color_estimates(first_frame)
        
        # estimate initial sand profile
        self.find_initial_sand_profile(first_frame)

        self.data['progress'] = 'Initialized'            
        self.log_event('init_end', 'Finished initializing the video analysis.')

    
    def process_video(self):
        """ processes the entire video """

        self.log_event('run1_begin', 'Start iterating through the frames.')
        
        self.debug_setup()
        self.setup_processing()

        try:
            # iterate over the video and analyze it
            for self.frame_id, frame in enumerate(display_progress(self.video_blurred)):
                
                if self.frame_id % self.params['colors.adaptation_interval'] == 0:
                    self.find_color_estimates(frame)
                
                if self.frame_id >= self.params['video.ignore_initial_frames']:
                    # find the mouse
                    self.find_mice(frame)
                    
                    # use the background to find the current sand profile and burrows
                    if self.frame_id % self.params['sand_profile.adaptation_interval'] == 0:
                        self.refine_sand_profile(self._background)
                        sand_profile = SandProfile(self.frame_id, self.sand_profile)
                        self.data['sand_profile'].append(sand_profile)
            
                if self.frame_id % self.params['burrows.adaptation_interval'] == 0:
                    self.burrow_finder.find_burrows(frame, self.explored_area, self.sand_profile)
            
                # update the background model
                self._update_background_model(frame)
                    
                # store some information in the debug dictionary
                self.debug_add_frame(frame)
                
        except KeyboardInterrupt:
            logging.info('Tracking has been interrupted by user.')
            self.log_event('run1_interrupted', 'Run has been interrupted.')
            
        else:
            self.log_event('run1_end', 'Finished iterating through the frames.')
        
        frames_analyzed = self.frame_id + 1
        if frames_analyzed == self.video.frame_count:
            self.data['progress'] = 'Finished first pass'
        else:
            self.data['progress'] = 'Partly finished first pass'
        self.data['video_analyzed']['frames_analyzed'] = frames_analyzed
                    
        # cleanup and write out of data
        self.debug_finalize()
        self.write_data()


    def setup_processing(self):
        """ sets up the processing of the video by initializing caches etc """
        
        self.data['objects.trajectories'] = []
        self.data['sand_profile'] = []

        # creates a simple template for matching with the mouse.
        # This template can be used to update the current mouse position based
        # on information about the changes in the video.
        # The template consists of a core region of maximal intensity and a ring
        # region with gradually decreasing intensity.
        
        # determine the sizes of the different regions
        size_core = self.params['mouse.model_radius']
        size_ring = 3*self.params['mouse.model_radius']
        size_total = size_core + size_ring

        # build a filter for finding the mouse position
        x, y = np.ogrid[-size_total:size_total + 1, -size_total:size_total + 1]
        r = np.sqrt(x**2 + y**2)

        # build the template
        mouse_template = (
            # inner circle of ones
            (r <= size_core).astype(float)
            # + outer region that falls off
            + np.exp(-((r - size_core)/size_core)**2)  # smooth function from 1 to 0
              * (size_core < r)          # mask on ring region
        )  
        
        self._cache['mouse.template'] = mouse_template
        
        # setup more cache variables
        video_shape = (self.video.size[1], self.video.size[0]) 
        self.explored_area = np.zeros(video_shape, np.uint8)
        self._cache['background.mask'] = np.ones(video_shape, np.double)
        
            
    #===========================================================================
    # FINDING THE CAGE
    #===========================================================================
    
    
    def find_cage(self, image):
        """ analyzes a single image and locates the mouse cage in it.
        Try to find a bounding box for the cage.
        The rectangle [top, left, height, width] enclosing the cage is returned. """
        
        # do automatic thresholding to find large, bright areas
        _, binarized = cv2.threshold(image, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

        # find the largest bright are, which should contain the cage
        cage_mask = get_largest_region(binarized)
        
        # find an enclosing rectangle, which usually overestimates the cage bounding box
        rect_large = find_bounding_box(cage_mask)
         
        # crop image to this rectangle, which should surely contain the cage 
        image = image[rect_to_slices(rect_large)]

        # initialize the rect coordinates
        top = 0 # start on first row
        bottom = image.shape[0] - 1 # start on last row
        width = image.shape[1]

        # threshold again, because large distractions outside of cages are now
        # definitely removed. Still, bright objects close to the cage, e.g. the
        # stands or some pipes in the background might distract the estimate.
        # We thus adjust the rectangle in the following  
        _, binarized = cv2.threshold(image, 0, 255,
                                     cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        
        # move top line down until we hit the cage boundary.
        # We move until more than 10% of the pixel on a horizontal line are bright
        brightness = binarized[top, :].sum()
        while brightness < 0.1*255*width: 
            top += 1
            brightness = binarized[top, :].sum()
        
        # move bottom line up until we hit the cage boundary
        # We move until more then 90% of the pixel of a horizontal line are bright
        brightness = binarized[bottom, :].sum()
        while brightness < 0.9*255*width: 
            bottom -= 1
            brightness = binarized[bottom, :].sum()

        # return the rectangle defined by two corner points
        p1 = (rect_large[0], rect_large[1] + top)
        p2 = (rect_large[0] + width - 1, rect_large[1] + bottom)
        return corners_to_rect(p1, p2)

  
    def crop_video_to_cage(self, user_crop):
        """ crops the video to a suitable cropping rectangle given by the cage """
        
        if user_crop is None:
            # use the full video
            if self.video.is_color:
                # restrict video to green channel if it is a color video
                video_crop = FilterMonochrome(self.video, 1)
            else:
                video_crop = self.video
                
            rect_given = [0, 0, self.video.size[0], self.video.size[1]]

        else: # user_crop is not None                
            # restrict video to green channel if it is a color video
            color_channel = 1 if self.video.is_color else None
            
            if isinstance(user_crop, str):
                # crop according to the supplied string
                video_crop = FilterCrop(self.video, quadrant=user_crop,
                                        color_channel=color_channel)
            else:
                # crop to the given rect
                video_crop = FilterCrop(self.video, rect=user_crop,
                                        color_channel=color_channel)

            rect_given = video_crop.rect
        
        # find the cage in the first frame of the movie
        blurred_image = FilterBlur(video_crop, self.params['video.blur_radius'])[0]
        rect_cage = self.find_cage(blurred_image)
        
        # determine the rectangle of the cage in global coordinates
        left = rect_given[0] + rect_cage[0]
        top = rect_given[1] + rect_cage[1]
        width = rect_cage[2] - rect_cage[2] % 2   # make sure its divisible by 2
        height = rect_cage[3] - rect_cage[3] % 2  # make sure its divisible by 2
        # Video dimensions should be divisible by two for some codecs

        if not (self.params['cage.width_min'] < width < self.params['cage.width_max'] and
                self.params['cage.height_min'] < height < self.params['cage.height_max']):
            raise RuntimeError('The cage bounding box (%dx%d) is out of the limits.' % (width, height)) 
        
        cropping_rect = [left, top, width, height]
        
        logging.debug('The cage was determined to lie in the rectangle %s', cropping_rect)

        # crop the video to the cage region
        if self.video.is_color:
            # restrict video to green channel
            self.video = FilterCrop(self.video, cropping_rect, color_channel=1)
        else:
            self.video = FilterCrop(self.video, cropping_rect)
            
        return cropping_rect
            
            
    #===========================================================================
    # BACKGROUND MODEL AND COLOR ESTIMATES
    #===========================================================================
               
               
    def find_color_estimates(self, image):
        """ estimate the colors in the sky region and the sand region """
        
        # add black border around image, which is important for the distance 
        # transform we use later
        image = cv2.copyMakeBorder(image, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=0)
        
        # binarize image
        _, binarized = cv2.threshold(image, 0, 1,
                                     cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        
        # find sky by locating the largest black areas
        sky_mask = get_largest_region(1 - binarized).astype(np.uint8)*255

        # Finding sure foreground area using a distance transform
        dist_transform = cv2.distanceTransform(sky_mask, cv2.cv.CV_DIST_L2, 5)
        if len(dist_transform) == 2:
            # fallback for old behavior of OpenCV, where an additional parameter
            # would be returned
            dist_transform = dist_transform[0]
        _, sky_sure = cv2.threshold(dist_transform, 0.25*dist_transform.max(), 255, 0)

        # determine the sky color
        sky_img = image[sky_sure.astype(np.bool)]
        self.data['colors'] = {}
        self.data['colors']['sky'] = sky_img.mean()
        self.data['colors']['sky_std'] = sky_img.std()
        logging.debug('The sky color was determined to be %d +- %d',
                      self.data['colors']['sky'], self.data['colors']['sky_std'])

        # find the sand by looking at the largest bright region
        sand_mask = get_largest_region(binarized).astype(np.uint8)*255
        
        # Finding sure foreground area using a distance transform
        dist_transform = cv2.distanceTransform(sand_mask, cv2.cv.CV_DIST_L2, 5)
        if len(dist_transform) == 2:
            # fallback for old behavior of OpenCV, where an additional parameter
            # would be returned
            dist_transform = dist_transform[0]
        _, sand_sure = cv2.threshold(dist_transform, 0.5*dist_transform.max(), 255, 0)
        
        # determine the sky color
        sand_img = image[sand_sure.astype(np.bool)]
        self.data['colors']['sand'] = sand_img.mean()
        self.data['colors']['sand_std'] = sand_img.std()
        logging.debug('The sand color was determined to be %d +- %d',
                      self.data['colors']['sand'], self.data['colors']['sand_std'])
        
        
    def _get_mouse_template_slices(self, pos, i_shape, t_shape):
        """ calculates the slices necessary to compare a template to an image.
        Here, we assume that the image is larger than the template.
        pos refers to the center of the template
        """
        
        # get dimensions to determine center position         
        t_top  = t_shape[0]//2
        t_left = t_shape[1]//2
        pos = (pos[0] - t_left, pos[1] - t_top)

        # get the dimensions of the overlapping region        
        h = min(t_shape[0], i_shape[0] - pos[1])
        w = min(t_shape[1], i_shape[1] - pos[0])
        if h <= 0 or w <= 0:
            raise RuntimeError('Template and image do not overlap')
        
        # get the leftmost point in both images
        if pos[0] >= 0:
            i_x, t_x = pos[0], 0
        elif pos[0] <= -t_shape[1]:
            raise RuntimeError('Template and image do not overlap')
        else: # pos[0] < 0:
            i_x, t_x = 0, -pos[0]
            w += pos[0]
            
        # get the upper point in both images
        if pos[1] >= 0:
            i_y, t_y = pos[1], 0
        elif pos[1] <= -t_shape[0]:
            raise RuntimeError('Template and image do not overlap')
        else: # pos[1] < 0:
            i_y, t_y = 0, -pos[1]
            h += pos[1]
            
        # build the slices used to extract the information
        return ((slice(i_y, i_y + h), slice(i_x, i_x + w)),  # slice for the image
                (slice(t_y, t_y + h), slice(t_x, t_x + w)))  # slice for the template
    
        
    def _update_background_model(self, frame):
        """ updates the background model using the current frame """
        
        if self._mouse_pos_estimate:
            # load some values from the cache
            mask = self._cache['background.mask']
            template = 1 - self._cache['mouse.template']
            mask.fill(1)
            
            # cut out holes from the mask for each mouse estimate
            for mouse_pos in self._mouse_pos_estimate:
                # get the slices required for comparing the template to the image
                i_s, t_s = self._get_mouse_template_slices(mouse_pos, frame.shape, template.shape)
                mask[i_s[0], i_s[1]] *= template[t_s[0], t_s[1]]
                
        else:
            # disable the mask if no mouse is known
            mask = 1

        # adapt the background to current frame, but only inside the mask 
        self._background += (self.params['background.adaptation_rate']  # adaptation rate 
                             *mask                                      # mask 
                             *(frame - self._background))               # difference to current frame

                        
    #===========================================================================
    # FINDING THE MOUSE
    #===========================================================================
      
    
    def _find_moving_features(self, frame):
        """ finds moving features in a frame.
        This works by building a model of the current background and subtracting
        this from the current frame. Everything that deviates significantly from
        the background must be moving. Here, we additionally only focus on 
        features that become brighter, i.e. move forward.
        """
        
        # prepare the kernel for morphological opening if necessary
        if 'find_moving_features.kernel_open' not in self._cache:
            self._cache['find_moving_features.kernel_open'] = \
                cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
            self._cache['find_moving_features.kernel_close'] = \
                cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
#                 cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (self.params['mouse.model_radius']//2,)*2)
                        
        # calculate the difference to the current background model
        # Note that the first operand determines the dtype of the result.
        diff = -self._background + frame 
        
        # find movement by comparing the difference to a threshold 
        mask_moving = (diff > self.params['mouse.intensity_threshold']*self.data['colors']['sky_std'])
        mask_moving = 255*mask_moving.astype(np.uint8)

        # perform morphological opening to remove noise
        cv2.morphologyEx(mask_moving, cv2.MORPH_OPEN, 
                         self._cache['find_moving_features.kernel_open'],
                         dst=mask_moving)     
        # perform morphological closing to join distinct features
        cv2.morphologyEx(mask_moving, cv2.MORPH_CLOSE, 
                         self._cache['find_moving_features.kernel_close'],
                         dst=mask_moving)

        # plot the contour of the movement if debug video is enabled
        if 'video' in self.debug:
            self.debug['video'].add_contour(mask_moving, color='g', copy=True)

        return mask_moving


    def _find_objects_in_binary_image(self, binary_image):
        """ finds objects in a binary image.
        Returns a list with characteristic properties
        """
        
        # find all distinct features and label them
        labels, num_features = ndimage.measurements.label(binary_image)
        self.debug['object_count'] = num_features

        # find large objects (which could be the mouse)
        objects = []
        max_area, max_pos = 0, None
        for label in xrange(1, num_features + 1):
            # calculate the image moments
            moments = cv2.moments((labels == label).astype(np.uint8))
            area = moments['m00']
            # get the coordinates of the center of mass
            pos = (moments['m10']/area, moments['m01']/area)
            
            # check whether this object could be a mouse
            if area > self.params['mouse.min_area']:
                objects.append(Object(pos, size=area))
                
            elif area > max_area:
                # determine the maximal area during the loop
                max_area, max_pos = area, pos
        
        if len(objects) == 0:
            # if we haven't found anything yet, we just take the best guess,
            # which is the object with the largest area
            objects.append(Object(max_pos, size=max_area))
        
        return objects

                
    def _find_best_template_position(self, image, template, start_pos, max_deviation=None):
        """
        moves a template around until it has the largest overlap with image
        start_pos refers to the the center of the template inside the image
        max_deviation sets a maximal distance the template is moved from start_pos
        """
        
        # lists for checking all points surrounding the current one
        points_to_check, seen = [start_pos], set()
        # variables storing the best fit
        best_overlap, best_pos = -np.inf, None
        
        # iterate over all possible points 
        while points_to_check:
            
            # get the next position to check, which refers to the top left image of the template
            pos = points_to_check.pop()
            seen.add(pos)

            # get the slices required for comparing the template to the image
            i_s, t_s = self._get_mouse_template_slices(pos, image.shape, template.shape)
                        
            # calculate the similarity
            overlap = (image[i_s[0], i_s[1]]*template[t_s[0], t_s[1]]).sum()
            
            # compare it to the previously seen one
            if overlap > best_overlap:
                best_overlap = overlap
                best_pos = pos
                
                # add points around the current one to the test list
                for p in ((pos[0] - 1, pos[1]), (pos[0], pos[1] - 1),
                          (pos[0] + 1, pos[1]), (pos[0], pos[1] + 1)):
                    
                    # points will only be added if they have not already been checked
                    # and if the associated distance is below the threshold
                    if (not p in seen 
                        and (max_deviation is None or 
                             np.hypot(p[0] - start_pos[0], 
                                      p[1] - start_pos[1]) < max_deviation)
                        ):
                        points_to_check.append(p)
                
        return best_pos
  

    def _handle_object_trajectories(self, frame, mask_moving):
        """ analyzes a single frame and tries to join object trajectories """
        # get potential positions
        objects_found = self._find_objects_in_binary_image(mask_moving)
        
        if len(self.mice) == 0:
            self.mice = [ObjectTrajectory(self.frame_id, mouse,
                                     moving_window=self.params['objects.matching_moving_window'])
                         for mouse in objects_found]
            
            return # there is nothing to do anymore
            
        # calculate the distance between new and old objects
        dist = distance.cdist([obj.pos for obj in objects_found],
                              [mouse.predict_pos() for mouse in self.mice],
                              metric='euclidean')
        # normalize distance to the maximum speed
        dist = dist/self.params['mouse.max_speed']
        
        # calculate the difference of areas between new and old objects
        def area_score(area1, area2):
            """ helper function scoring area differences """
            return abs(area1 - area2)/(area1 + area2)
        
        areas = np.array([[area_score(obj.size, mouse.last_size)
                           for mouse in self.mice]
                          for obj in objects_found])
        # normalize area change such that 1 corresponds to the maximal allowed one
        areas = areas/self.params['mouse.max_rel_area_change']

        # build a combined score from this
        alpha = self.params['objects.matching_weigth']
        score = alpha*dist + (1 - alpha)*areas

        # match previous estimates to this one
        idx_f = range(len(objects_found)) # indices of new objects
        idx_e = range(len(self.mice))     # indices of old objects
        while True:
            # get the smallest score, which corresponds to best match            
            score_min = score.min()
            
            if score_min > 1:
                # there are no good matches left
                break
            
            else:
                # find the indices of the match
                i_f, i_e = np.argwhere(score == score_min)[0]
                
                # append new object to the trajectory of the old object
                self.mice[i_e].append(self.frame_id, objects_found[i_f])
                
                # eliminate both objects from further considerations
                score[i_f, :] = np.inf
                score[:, i_e] = np.inf
                idx_f.remove(i_f)
                idx_e.remove(i_e)
                
        # end trajectories that had no match in current frame 
        for i_e in reversed(idx_e): # have to go backwards, since we delete items
            logging.debug('%d: Copy mouse trajectory of length %d to results',
                          self.frame_id, len(self.mice[i_e]))
            # copy trajectory to result dictionary
            self.data['objects.trajectories'].append(self.mice[i_e])
            del self.mice[i_e]
        
        # start new trajectories for objects that had no previous match
        for i_f in idx_f:
            logging.debug('%d: Start new mouse trajectory at %s', self.frame_id, objects_found[i_f].pos)
            # start new trajectory
            self.mice.append(ObjectTrajectory(self.frame_id, objects_found[i_f],
                                         moving_window=self.params['objects.matching_moving_window']))
        
        assert len(self.mice) == len(objects_found)
        
    
    def find_mice(self, frame):
        """ adapts the current mouse position, if enough information is available """

        # find features that indicate that the mouse moved
        mask_moving = self._find_moving_features(frame)
        
        if mask_moving.sum() == 0:
            # end all current trajectories if there are any
            if len(self.mice) > 0:
                logging.debug('%d: Copy %d trajectories to results', 
                              self.frame_id, len(self.mice))
                self.data['objects.trajectories'].extend(self.mice)
                self.mice = []

        else:
            # some moving features have been found in the video 
            self._handle_object_trajectories(frame, mask_moving)
            
            # check whether objects moved and call them a mouse
            mice_moving = [mouse.is_moving() for mouse in self.mice]
            if any(mice_moving):
                self.data['mouse.has_moved'] = True
                # remove the trajectories that didn't move
                # this essentially assumes that there is only one mouse
                self.mice = [mouse
                             for k, mouse in enumerate(self.mice)
                             if mice_moving[k]]

            self._mouse_pos_estimate = [mouse.last_pos for mouse in self.mice]
        
        # keep track of the regions that the mouse explored
        for mouse in self.mice:
            cv2.circle(self.explored_area, mouse.last_pos,
                       radius=self.params['burrows.radius'],
                       color=255, thickness=-1)
                                
                
    #===========================================================================
    # FINDING THE SAND PROFILE
    #===========================================================================
    
    
    def _find_rough_sand_profile(self, image):
        """ determines an estimate of the sand profile from a single image """
        
        # remove 10%/15% of each side of the image
        h = int(0.15*image.shape[0])
        w = int(0.10*image.shape[1])
        image_center = image[h:-h, w:-w]
        
        # binarize image
        cv2.threshold(image_center, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU, dst=image_center)
        
        # TODO: we might want to replace that with the typical burrow radius
        # do morphological opening and closing to smooth the profile
        s = 4*self.params['sand_profile.point_spacing']
        ys, xs = np.ogrid[-s:s+1, -s:s+1]
        kernel = (xs**2 + ys**2 <= s**2).astype(np.uint8)

        # widen the mask
        mask = cv2.copyMakeBorder(image_center, s, s, s, s, cv2.BORDER_REPLICATE)
        # make sure their is sky on the top
        mask[:s + h, :] = 0
        # make sure their is and at the bottom
        mask[-s - h:, :] = 255

        # morphological opening to remove noise and clutter
        cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, dst=mask)

        # morphological closing to smooth the boundary and remove burrows
        cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, borderType=cv2.BORDER_REPLICATE, dst=mask)
        
        # reduce the mask to its original dimension
        mask = mask[s:-s, s:-s]
        
        # get the contour from the mask and store points as (x, y)
        points = [(x + w, np.nonzero(col)[0][0] + h)
                  for x, col in enumerate(mask.T)]            

        # simplify the curve        
        points = simplify_curve(points, epsilon=2)

        return np.array(points, np.int)
   
        
    def refine_sand_profile(self, image):
        """ adapts a sand profile given as points to a given image.
        Here, we fit a ridge profile in the vicinity of every point of the curve.
        The only fitting parameter is the distance by which a single points moves
        in the direction perpendicular to the curve. """
                
        points = self.sand_profile
        spacing = self.params['sand_profile.point_spacing']
            
        if not 'sand_profile.model' in self._cache or \
            self._cache['sand_profile.model'].size != spacing:
            
            self._cache['sand_profile.model'] = \
                    RidgeProfile(spacing, self.params['sand_profile.width'])
                
        # make sure the curve has equidistant points
        sand_profile = make_curve_equidistant(points, spacing)
        sand_profile = np.array(np.round(sand_profile),  np.int)
        
        # calculate the bounds for the points
        p_min =  spacing 
        y_max, x_max = image.shape[0] - spacing, image.shape[1] - spacing

        # iterate through all points and correct profile
        sand_profile_model = self._cache['sand_profile.model']
        corrected_points = []
        x_previous = spacing
        for k, p in enumerate(sand_profile):
            
            # skip points that are too close to the boundary
            if (p[0] < p_min or p[0] > x_max or 
                p[1] < p_min or p[1] > y_max):
                continue
            
            # determine the local slope of the profile, which fixes the angle 
            if k == 0 or k == len(sand_profile) - 1:
                # we only move these vertically to prevent the sand profile
                # from shortening
                angle = np.pi/2
            else:
                dp = sand_profile[k+1] - sand_profile[k-1]
                angle = np.arctan2(dp[0], dp[1]) # y-coord, x-coord
                
            # extract the region around the point used for fitting
            region = image[p[1]-spacing : p[1]+spacing+1, p[0]-spacing : p[0]+spacing+1].copy()
            sand_profile_model.set_data(region, angle) 

            # move the sand profile model perpendicular until it fits best
            x, _, infodict, _, _ = \
                leastsq(sand_profile_model.get_difference, [0], xtol=0.1, full_output=True)
            
            # calculate goodness of fit
            ss_err = (infodict['fvec']**2).sum()
            ss_tot = ((region - region.mean())**2).sum()
            if ss_tot == 0:
                rsquared = 0
            else:
                rsquared = 1 - ss_err/ss_tot

            # Note, that we never remove the first and the last point
            if rsquared > 0.1 or k == 0 or k == len(sand_profile) - 1:
                # we are rather confident that this point is better than it was
                # before and thus add it to the result list
                p_x = p[0] + x[0]*np.cos(angle)
                p_y = p[1] - x[0]*np.sin(angle)
                # make sure that we have no overhanging ridges
                if p_x >= x_previous:
                    corrected_points.append((int(p_x), int(p_y)))
                    x_previous = p_x
            
        self.sand_profile = np.array(corrected_points)
            

    def find_initial_sand_profile(self, image):
        """ finds the sand profile given an image of an antfarm """

        # get an estimate of a profile
        self.sand_profile = self._find_rough_sand_profile(image)
        
        # iterate until the profile does not change significantly anymore
        length_last, length_current = 0, curve_length(self.sand_profile)
        iterations = 0
        while abs(length_current - length_last)/length_current > 0.001 or iterations < 5:
            self.refine_sand_profile(image)
            length_last, length_current = length_current, curve_length(self.sand_profile)
            iterations += 1
            
        logging.info('We found a sand profile of length %g after %d iterations',
                     length_current, iterations)
        

    #===========================================================================
    # DEBUGGING
    #===========================================================================


    def log_event(self, name, description):
        """ stores and/or outputs the time and date of the event given by name """
        now = datetime.datetime.now() 
            
        # log the event
        logging.info(str(now) + ': ' + description)
        
        # save the event in the result structure
        if 'events' not in self.data:
            self.data['events'] = {}
        self.data['events'][name] = now


    def debug_setup(self):
        """ prepares everything for the debug output """
        self.debug['object_count'] = 0
        self.debug['text1'] = ''
        self.debug['text2'] = ''
        
        # set up the general video output, if requested
        if 'video' in self.debug_output or 'video.show' in self.debug_output:
            # initialize the writer for the debug video
            debug_file = self.get_filename('video' + self.video_output_extension, 'debug')
            self.debug['video'] = VideoComposerListener(debug_file, background=self.video,
                                                        is_color=True, codec=self.video_output_codec,
                                                        bitrate=self.video_output_bitrate)
            if 'video.show' in self.debug_output:
                self.debug['video.show'] = ImageShow(self.debug['video'].shape, 'Debug video')

        # set up additional video writers
        for identifier in ('difference', 'background', 'explored_area'):
            if identifier in self.debug_output:
                # determine the filename to be used
                debug_file = self.get_filename(identifier + self.video_output_extension, 'debug')
                # set up the video file writer
                video_writer = VideoComposer(debug_file, self.video.size, self.video.fps,
                                               is_color=False, codec=self.video_output_codec,
                                               bitrate=self.video_output_bitrate)
                self.debug[identifier + '.video'] = video_writer
        

    def debug_add_frame(self, frame):
        """ adds information of the current frame to the debug output """
        
        if 'video' in self.debug:
            debug_video = self.debug['video']
            
            # plot the sand profile
            debug_video.add_polygon(self.sand_profile, is_closed=False, color='y')
            debug_video.add_points(self.sand_profile, radius=2, color='y')
        
            # indicate the mouse position
            if len(self.mice) > 0:
                for mouse in self.mice:
                    if not self.data['mouse.has_moved']:
                        color = 'r'
                    elif mouse.is_moving():
                        color = 'w'
                    else:
                        color = 'b'
                        
                    debug_video.add_polygon(mouse.get_trajectory(), '0.5', is_closed=False)
                    debug_video.add_circle(mouse.last_pos, self.params['mouse.model_radius'], color, thickness=1)
                
            else: # there are no current trajectories
                for mouse_pos in self._mouse_pos_estimate:
                    debug_video.add_circle(mouse_pos, self.params['mouse.model_radius'], 'k', thickness=1)
             
            debug_video.add_text(str(self.frame_id), (20, 20), anchor='top')   
            debug_video.add_text('#objects:%d' % self.debug['object_count'], (120, 20), anchor='top')
            debug_video.add_text(self.debug['text1'], (300, 20), anchor='top')
            debug_video.add_text(self.debug['text2'], (300, 50), anchor='top')
            
            if 'video.show' in self.debug:
                self.debug['video.show'].show(debug_video.frame)
                
        if 'difference.video' in self.debug:
            diff = np.clip(frame.astype(int) - self._background + 128, 0, 255)
            self.debug['difference.video'].write_frame(diff.astype(np.uint8))
                
        if 'background.video' in self.debug:
            self.debug['background.video'].write_frame(self._background)

        if 'explored_area.video' in self.debug:
            debug_video = self.debug['explored_area.video']
             
            # set the background
            debug_video.set_frame(128*self.explored_area)
            
            # plot the sand profile
            debug_video.add_polygon(self.sand_profile, is_closed=False, color='y')
            debug_video.add_points(self.sand_profile, radius=2, color='y')


    def debug_finalize(self):
        """ close the video streams when done iterating """
        self.debug.clear()
        # remove all windows that may have been opened
        cv2.destroyAllWindows()
            
    
    
class RidgeProfile(object):
    """ represents a ridge profile to compare it against an image in fitting """
    
    def __init__(self, size, profile_width=1):
        """ initialize the structure
        size is half the width of the region of interest
        profile_width determines the blurriness of the ridge
        """
        self.size = size
        self.ys, self.xs = np.ogrid[-size:size+1, -size:size+1]
        self.width = profile_width
        self.image = None
        
        
    def set_data(self, image, angle):
        """ sets initial data used for fitting
        image denotes the data we compare the model to
        angle defines the direction perpendicular to the profile 
        """
        
        self.image = image
        self.image_mean = image.mean()
        self.image_std = image.std()
        self._sina = np.sin(angle)
        self._cosa = np.cos(angle)
        
        
    def get_difference(self, distance):
        """ calculates the difference between image and model, when the 
        model is moved by a certain distance in its normal direction """ 
        # determine center point
        px =  distance*self._cosa
        py = -distance*self._sina
        
        # determine the distance from the ridge line
        dist = (self.ys - py)*self._sina - (self.xs - px)*self._cosa + 0.5 # TODO: check the 0.5
        
        # apply sigmoidal function
        model = np.tanh(dist/self.width)
     
        return np.ravel(self.image_mean + 1.5*self.image_std*model - self.image)

