#!/usr/bin/env python
"""
Web app for maintaining all of your video datasets:
- Setup new datasets with custom labels and temporal tags
- Record new videos (coming soon)
- Temporally annotate your videos with custom tags
- Train custom models using strong backbone networks (coming soon)
"""

import glob
import json
import multiprocessing
import os
import urllib

import ffmpeg
from flask import Flask
from flask import jsonify
from flask import redirect
from flask import render_template
from flask import request
from flask import url_for
from natsort import natsorted
from natsort import ns

from sense import SPLITS
from tools import directories
from tools.sense_studio import project_utils
from tools.sense_studio import socketio
from tools.sense_studio.annotation import annotation_bp
from tools.sense_studio.demos import demos_bp
from tools.sense_studio.annotation import train_logreg
from tools.sense_studio.testing import testing_bp
from tools.sense_studio.training import training_bp
from tools.sense_studio.video_recording import video_recording_bp
from tools.sense_studio.tags import tags_bp

app = Flask(__name__)
app.secret_key = 'd66HR8dç"f_-àgjYYic*dh'
app.debug = True

app.register_blueprint(annotation_bp, url_prefix='/annotation')
app.register_blueprint(video_recording_bp, url_prefix='/video-recording')
app.register_blueprint(training_bp, url_prefix='/training')
app.register_blueprint(testing_bp, url_prefix='/testing')
app.register_blueprint(tags_bp, url_prefix='/tags')
app.register_blueprint(demos_bp, url_prefix='/demos')

socketio.init_app(app)

# Training script expects videos in MP4 format
VIDEO_EXT = '.mp4'


@app.route('/')
def projects_overview():
    """
    Home page of SenseStudio. Show the overview of all registered projects and check if their
    locations are still valid.
    """
    projects = project_utils.load_project_overview_config()

    # Check if project paths still exist
    for project in projects.values():
        project['exists'] = os.path.exists(project['path'])

    return render_template('projects_overview.html', projects=projects)


@app.route('/project-config', methods=['POST'])
def project_config():
    """
    Provide the config for a given project.
    """
    data = request.json
    name = data['name']
    path = project_utils.lookup_project_path(name)

    # Get config
    config = project_utils.load_project_config(path)
    return jsonify(config)


@app.route('/remove-project/<string:name>')
def remove_project(name):
    """
    Remove a given project from the config file and reload the overview page.
    """
    name = urllib.parse.unquote(name)
    projects = project_utils.load_project_overview_config()

    del projects[name]

    project_utils.write_project_overview_config(projects)

    return redirect(url_for('projects_overview'))


@app.route('/browse-directory', methods=['POST'])
def browse_directory():
    """
    Browse the local file system starting at the given path and provide the following information:
    - project_name_unique: If the given project name is not yet registered in the projects list
    - project_path_prefix: The given path with a final separator, e.g. /data/
    - project_dir: Name of the project directory generated from the project name
    - project_dir_exists: If the project directory already exists in the given path
    - path_exists: If the given path exists
    - path_unique: If the given path is not yet registered for another project
    - subdirs: The list of sub-directories at the given path
    """
    data = request.json
    path = data['path']
    project = data['project']

    subdirs = [d for d in glob.glob(f'{path}*') if os.path.isdir(d)] if os.path.isabs(path) else []
    project_dir = project_utils.get_folder_name_for_project(project)
    full_path = os.path.join(path, project_dir)

    video_files = list(glob.glob(f'{path}*{VIDEO_EXT}'))
    projects = project_utils.load_project_overview_config()

    return jsonify(
        project_name_unique=project not in projects,
        project_path_prefix=os.path.join(path, ''),  # Append a separator
        project_dir=project_dir,
        project_dir_exists=os.path.exists(full_path),
        path_exists=os.path.exists(path),
        path_unique=path not in [p['path'] for p in projects.values()],
        subdirs=subdirs,
        video_files=video_files,
    )


@app.route('/create-project', methods=['POST'])
def create_project():
    """
    Setup a new project directory and add it to the projects overview config file.
    The given project name will be used for constructing the directory in the given path.
    """
    data = request.form
    project_name = data['projectName']
    path = data['path']

    path = os.path.join(path, project_utils.get_folder_name_for_project(project_name))
    os.mkdir(path)

    # Setup new project
    project_utils.setup_new_project(project_name, path)

    return redirect(url_for('project_details', project=project_name))


@app.route('/update-project', methods=['POST'])
def update_project():
    """
    Update an existing project entry with a new path. If a config file exists in there, it will be
    used, otherwise a new one will be created.
    The project will keep the given project name.
    """
    data = request.form
    project_name = data['projectName']
    path = data['path']

    # Check for existing config file (might be None)
    config = project_utils.load_project_config(path)

    # Make sure the directory is correctly set up
    project_utils.setup_new_project(project_name, path, config)

    return redirect(url_for('project_details', project=project_name))


@app.route('/import-project', methods=['POST'])
def import_project():
    """
    Import an existing project from the given path. If a config file exists in there, it will be
    used while also making sure that the project name is still unique. Otherwise, a new config
    will be created and a unique project name will be constructed from the directory name.
    """
    data = request.form
    path = data['path']

    # Check for existing config file and make sure project name is unique
    config = project_utils.load_project_config(path)
    if config:
        project_name = project_utils.get_unique_project_name(config['name'])
    else:
        # Use folder name as project name and make sure it is unique
        project_name = project_utils.get_unique_project_name(os.path.basename(path))

    # Make sure the directory is correctly set up
    project_utils.setup_new_project(project_name, path, config)

    return redirect(url_for('project_details', project=project_name))


@app.route('/project/<string:project>')
def project_details(project):
    """
    Show the details for the selected project.
    """
    project = urllib.parse.unquote(project)
    path = project_utils.lookup_project_path(project)
    config = project_utils.load_project_config(path)

    stats = {}
    for class_name in config['classes']:
        stats[class_name] = {}
        for split in SPLITS:
            videos_dir = directories.get_videos_dir(path, split, class_name)
            tags_dir = directories.get_tags_dir(path, split, class_name)
            stats[class_name][split] = {
                'total': len(os.listdir(videos_dir)),
                'tagged': len(os.listdir(tags_dir)) if os.path.exists(tags_dir) else 0,
                'videos': natsorted([video for video in os.listdir(videos_dir) if video.endswith(VIDEO_EXT)], alg=ns.IC)
            }
    tags = config['tags']
    return render_template('project_details.html', config=config, path=path, stats=stats, project=config['name'],
                           tags=tags)


@app.route('/add-class/<string:project>', methods=['POST'])
def add_class(project):
    """
    Add a new class to the given project.
    """
    data = request.form
    project = urllib.parse.unquote(project)
    path = project_utils.lookup_project_path(project)
    class_name = data['className']

    # Update project config
    config = project_utils.load_project_config(path)
    config['classes'][class_name] = []
    project_utils.write_project_config(path, config)

    # Setup directory structure
    for split in SPLITS:
        videos_dir = directories.get_videos_dir(path, split, class_name)

        if not os.path.exists(videos_dir):
            os.mkdir(videos_dir)

    return redirect(url_for("project_details", project=project))


@app.route('/toggle-project-setting', methods=['POST'])
def toggle_project_setting():
    """
    Toggle boolean project setting.
    """
    data = request.json
    path = data['path']
    setting = data['setting']
    new_status = project_utils.toggle_project_setting(path, setting)

    # Update logreg model if assisted tagging was just enabled
    if setting == 'assisted_tagging' and new_status:
        train_logreg(path=path)

    return jsonify(setting_status=new_status)


@app.route('/edit-class/<string:project>/<string:class_name>', methods=['POST'])
def edit_class(project, class_name):
    """
    Edit the name for an existing class in the given project.
    """
    data = request.form
    project = urllib.parse.unquote(project)
    class_name = urllib.parse.unquote(class_name)
    path = project_utils.lookup_project_path(project)
    new_class_name = data['className']

    # Update project config
    config = project_utils.load_project_config(path)
    tags = config['classes'][class_name]

    del config['classes'][class_name]
    config['classes'][new_class_name] = tags
    project_utils.write_project_config(path, config)

    # Update directory names
    data_dirs = []
    for split in SPLITS:
        data_dirs.extend([
            directories.get_videos_dir(path, split),
            directories.get_frames_dir(path, split),
            directories.get_tags_dir(path, split),
        ])

        # Feature directories follow the format <dataset_dir>/<split>/<model>/<num_layers_to_finetune>/<label>
        features_dir = directories.get_features_dir(path, split)
        if os.path.exists(features_dir):
            model_dirs = [os.path.join(features_dir, model_dir) for model_dir in os.listdir(features_dir)]
            data_dirs.extend([os.path.join(model_dir, tuned_layers)
                              for model_dir in model_dirs
                              for tuned_layers in os.listdir(model_dir)])

    for base_dir in data_dirs:
        class_dir = os.path.join(base_dir, class_name)

        if os.path.exists(class_dir):
            new_class_dir = os.path.join(base_dir, new_class_name)
            os.rename(class_dir, new_class_dir)

    return redirect(url_for('project_details', project=project))


@app.route('/remove-class/<string:project>/<string:class_name>')
def remove_class(project, class_name):
    """
    Remove the given class from the config file of the given project. No data will be deleted.
    """
    project = urllib.parse.unquote(project)
    class_name = urllib.parse.unquote(class_name)
    path = project_utils.lookup_project_path(project)

    # Update project config
    config = project_utils.load_project_config(path)
    del config['classes'][class_name]
    project_utils.write_project_config(path, config)

    return redirect(url_for("project_details", project=project))


@app.route('/assign-tag-to-class', methods=['POST'])
def assign_tag_to_class():
    """
    Assign selected tag to class label in project config.
    """
    data = request.json
    path = data['path']
    tag_index = data['tagIndex']
    class_name = data['className']

    config = project_utils.load_project_config(path)
    class_tags = config['classes'][class_name]
    class_tags.append(int(tag_index))
    class_tags.sort()

    project_utils.write_project_config(path, config)
    return jsonify(success=True)


@app.route('/remove-tag-from-class', methods=['POST'])
def remove_tag_from_class():
    """
    Remove selected tag from class label in project config.
    """
    data = request.json
    path = data['path']
    tag_index = data['tagIndex']
    class_name = data['className']

    config = project_utils.load_project_config(path)
    config['classes'][class_name].remove(int(tag_index))

    project_utils.write_project_config(path, config)
    return jsonify(success=True)


@app.after_request
def add_header(r):
    """
    Add headers to both force latest IE rendering engine or Chrome Frame,
    and also to cache the rendered page for 10 minutes.
    """
    r.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    r.headers["Pragma"] = "no-cache"
    r.headers["Expires"] = "0"
    r.headers['Cache-Control'] = 'public, max-age=0'
    return r


@app.context_processor
def context_processors():
    """
    This context processor will inject methods into templates,
    which can be invoked like an ordinary method in HTML templates.
    E.g. {% set project_config = inject_project_config(project) %}
    """
    def inject_project_config(project):
        path = project_utils.lookup_project_path(project)
        return project_utils.load_project_config(path)

    return dict(inject_project_config=inject_project_config)


@app.route('/set-timer-default', methods=['POST'])
def set_timer_default():
    data = request.json
    path = data['path']
    countdown = int(data['countdown'])
    recording = int(data['recording'])

    project_utils.set_timer_default(path, countdown, recording)

    return jsonify(status=True)


@app.route('/flip-videos', methods=['POST'])
def flip_videos():
    """
    Flip the videos horizontally for given class and
    copy tags of selected original videos for flipped version of it.
    """
    data = request.json
    project = data['projectName']
    path = project_utils.lookup_project_path(project)
    config = project_utils.load_project_config(path)
    counterpart_class_name = str(data['counterpartClassName'])
    original_class_name = str(data['originalClassName'])
    copy_video_tags = data['videosToCopyTags']

    if counterpart_class_name not in config['classes']:
        config['classes'][counterpart_class_name] = config['classes'][original_class_name] \
            if copy_video_tags['train'] or copy_video_tags['valid'] else []
        project_utils.write_project_config(path, config)

    for split in SPLITS:
        videos_path_in = os.path.join(path, f'videos_{split}', original_class_name)
        videos_path_out = os.path.join(path, f'videos_{split}', counterpart_class_name)
        original_tags_path = os.path.join(path, f'tags_{split}', original_class_name)
        counterpart_tags_path = os.path.join(path, f'tags_{split}', counterpart_class_name)

        # Create directory to save flipped videos
        os.makedirs(videos_path_out, exist_ok=True)
        os.makedirs(counterpart_tags_path, exist_ok=True)

        video_list = [video for video in os.listdir(videos_path_in) if video.endswith(VIDEO_EXT)]

        for video in video_list:
            output_video_list = [op_video for op_video in os.listdir(videos_path_out) if op_video.endswith(VIDEO_EXT)]
            print(f'Processing video: {video}')
            if '_flipped' in video:
                flipped_video_name = ''.join(video.split('_flipped'))
            else:
                flipped_video_name = video.split('.')[0] + '_flipped' + VIDEO_EXT

            if flipped_video_name not in output_video_list:
                # Original video as input
                original_video = ffmpeg.input(os.path.join(videos_path_in, video))
                # Do horizontal flip
                flipped_video = ffmpeg.hflip(original_video)
                # Get flipped video output
                flipped_video_output = ffmpeg.output(flipped_video,
                                                     filename=os.path.join(videos_path_out, flipped_video_name))
                # Run to render and save video
                ffmpeg.run(flipped_video_output)

                # Copy tags of original video to flipped video (in train/valid set)
                if video in copy_video_tags[split]:
                    original_tags_file = os.path.join(original_tags_path, video.replace(VIDEO_EXT, '.json'))
                    flipped_tags_file = os.path.join(counterpart_tags_path,
                                                     flipped_video_name.replace(VIDEO_EXT, '.json'))

                    if os.path.exists(original_tags_file):
                        with open(original_tags_file) as f:
                            original_video_tags = json.load(f)
                        original_video_tags['file'] = flipped_video_name
                        with open(flipped_tags_file, 'w') as f:
                            f.write(json.dumps(original_video_tags, indent=2))

    print("Processing complete!")
    return jsonify(status=True, url=url_for("project_details", project=project))


if __name__ == '__main__':
    multiprocessing.set_start_method('spawn')
    socketio.run(app)
