import os
import sys
import configparser
import filecmp
import logging
import glob
import pickle
import math
import pytest
from shutil import rmtree
from difflib import unified_diff

import numpy as np

from smoderp2d.providers.base import WorkflowMode
from smoderp2d.providers import Logger


def write_array_diff_png(diff, target_path):
    import matplotlib.pyplot as plt
    import matplotlib.colors as mcolors

    vmin = diff.min()
    if vmin > 0:
        vmin *= -1
    elif vmin == 0:
        vmin = -1 * diff.max()

    vmax = diff.max()
    vcenter = 0 if vmax > 0 else (vmax - abs(vmin)) / 2
    if not math.isclose(vmin, vmax):
        norm = mcolors.TwoSlopeNorm(vmin=vmin, vcenter=vcenter, vmax=vmax)
    else:
        norm = None
    plt.imshow(diff.astype(int), cmap="bwr", norm=norm)
    plt.colorbar()
    plt.savefig(os.path.join(target_path + ".diff.png"))
    plt.clf()


def write_array_diff(arr1, arr2, target_path):
    try:
        diff = arr1 - arr2
    except ValueError as e:
        if arr1.shape == arr2.shape:
            print(f"Unable to compute array diff: {e}")
        else:
            print(
                f"The two arrays have different shapes: "
                f"{arr1.shape} versus {arr2.shape}"
            )
        return

    if not diff.any():
        return

    # print statistics
    sys.stdout.writelines("\tdiff_stats ({}) min: {} max: {} mean:{}\n".format(
        os.path.basename(target_path), diff.min(), diff.max(), diff.mean()))

    with open(target_path + ".diff", "w") as fd:
        np.savetxt(fd, diff)

    write_array_diff_png(diff, target_path)


def are_dir_trees_equal(dir1, dir2):
    """
    Taken from https://stackoverflow.com/questions/4187564/recursively-compare-two-directories-to-ensure-they-have-the-same-files-and-subdi
    Compare two directories recursively. Files in each directory are
    assumed to be equal if their names and contents are equal.

    @param dir1: First directory path
    @param dir2: Second directory path

    @return: True if the directory trees are the same and
        there were no errors while accessing the directories or files,
        False otherwise.
    """
    def _read_gdal_array(filename):
        from osgeo import gdal
        ds = gdal.Open(filename)
        array = ds.GetRasterBand(1).ReadAsArray()
        ds = None
        return array

    def _read_data(filename):
        header_rows = 0
        delimiter = None
        with open(filename) as f:
            first_line = f.readline()
            use_gdal = first_line.startswith('ncols')
            is_point_csv = first_line.startswith('# Hydro')
        if use_gdal:
            return _read_gdal_array(filename)
        elif is_point_csv:
            header_rows = 3
            delimiter = ';'

        return np.loadtxt(filename, skiprows=header_rows, delimiter=delimiter)

    def _print_diff_files(same_files, diff_files, output_dir, ref_dir):
        for name in same_files:
            print(
                "same_file {} found in {} and {}".format(
                    name, output_dir, ref_dir
                )
            )
        for name in diff_files:
            diff_file = os.path.join(output_dir, name) + '.diff'
            print(
                "diff_file {} found in {} and {} -> {}".format(
                    name, output_dir, ref_dir, diff_file
                )
            )
            with open(os.path.join(output_dir, name)) as left:
                with open(os.path.join(ref_dir, name)) as right:
                    if _is_on_github_action():
                        fd = sys.stdout
                    else:
                        fd = open(diff_file, 'w')
                    fd.writelines(
                        unified_diff(left.readlines(), right.readlines())
                    )
                    if not _is_on_github_action():
                        fd.close()

            if not _is_on_github_action() and \
               os.path.splitext(name)[-1] == '.asc':
                write_array_diff(
                    # output generated by CmdProvider/GISProvider
                    _read_data(os.path.join(output_dir, name)),
                    # reference generated by GISProvider
                    _read_data(os.path.join(ref_dir, name)),
                    os.path.join(output_dir, name)
                )

    # https://stackoverflow.com/questions/46281434/python-filecmp-dircmp-ignore-wildcard
    def _ignore_list(left, right):
        ignore_list = ['temp']
        patterns_to_ignore = ['*.prj', '*.aux.xml']
        for pattern in patterns_to_ignore:
            left_files = glob.glob(os.path.join(left, pattern))
            ignore_left = [
                os.path.split(expanded)[1] for expanded in left_files
            ]
            right_files = glob.glob(os.path.join(right, pattern))
            ignore_right = [
                os.path.split(expanded)[1] for expanded in right_files
            ]
            ignore_list.extend(ignore_left)
            ignore_list.extend(ignore_right)
        return ignore_list

    relative_tolerance = 0.0001
    same_files = []
    diff_files = []

    for i in glob.glob(os.path.join(dir1, '*.asc')):
        file_path = os.path.split(i)[1]

        new_output = _read_data(i)
        reference = _read_data(os.path.join(dir2, file_path))
        if new_output.shape == reference.shape:
            equal = np.allclose(new_output, reference, rtol=relative_tolerance)
            if equal is True:
                same_files.append(file_path)
                continue

        diff_files.append(file_path)

    for i in glob.glob(os.path.join(dir1, 'control', '*.asc')):
        file_path = os.path.join('control', os.path.split(i)[1])

        new_output = _read_data(i)
        reference = _read_data(os.path.join(dir2, file_path))
        if new_output.shape == reference.shape:
            equal = np.allclose(new_output, reference, rtol=relative_tolerance)
            if equal is True:
                same_files.append(file_path)
                continue

        diff_files.append(file_path)

    for i in glob.glob(os.path.join(dir1, 'control_point', '*.csv')):
        file_path = os.path.join('control_point', os.path.split(i)[1])

        new_output = _read_data(i)
        reference = _read_data(os.path.join(dir2, file_path))
        if new_output.shape == reference.shape:
            equal = np.allclose(new_output, reference, rtol=relative_tolerance)
            if equal is True:
                same_files.append(file_path)
                continue

        diff_files.append(file_path)

    assert len(diff_files) == 0, \
        _print_diff_files(
            same_files, diff_files, dir1, dir2
        )

    return True


def _setup(request, config_file, reference_dir=None):
    request.cls.config_file = config_file
    request.cls.reference_dir = reference_dir

@pytest.fixture(scope='class')
def class_manager(request, pytestconfig):
    request.cls.reference_dir = pytestconfig.getoption("reference_dir")
    yield 

def _is_on_github_action():
    # https://docs.github.com/en/actions/learn-github-actions/variables
    if "GITHUB_ACTION" in os.environ:
        return True
    return False


data_dir = os.path.join(os.path.dirname(__file__), "data")


class PerformTest:

    def __init__(self, runner, reference_dir=None, params=None):
        self.runner = runner
        self.reference_dir = reference_dir
        self._output_dir = os.path.join(data_dir, "output")

        if params:
            self._params = {
                "soil_type_fieldname": "Soil",
                "vegetation_type_fieldname": "LandUse",
                "points_fieldname": "point_id",
                "rainfall_file": os.path.join(
                    data_dir, f"rainfall_{self.reference_dir}.txt"
                ),
                "maxdt": 30,
                "end_time": 40,
                "table_soil_vegetation_fieldname": "soilveg",
                "streams_channel_type_fieldname": "channel_id",
                "output": self._output_dir,
                'generate_temporary': False,
                'flow_direction': 'single'
            }
            self._params.update(params)
        else:
            self._params = None

    @staticmethod
    def _extract_pickle_data(data_dict, target_dir):
        if os.path.exists(target_dir):
            rmtree(target_dir)
        os.makedirs(target_dir)
        for k, v in data_dict.items():
            with open(os.path.join(target_dir, k), "w") as fd:
                if isinstance(v, np.ndarray):
                    np.savetxt(fd, v)
                else:
                    fd.write(str(v))

    @staticmethod
    def _extract_target_dir(path):
        return os.path.join(
            os.path.dirname(path),
            os.path.splitext(os.path.basename(path))[0] + ".extracted",
        )

    @staticmethod
    def _data_to_str(data_dict):
        return [
            "{}:{}\n".format(
                key,
                np.array2string(value, threshold=np.inf)
                if isinstance(value, np.ndarray)
                else value,
            )
            for (key, value) in sorted(data_dict.items())
        ]

    @staticmethod
    def _compare_arrays(new_output_dict, reference_dict, target_dir):
        for k, v in new_output_dict.items():
            if not isinstance(v, np.ndarray):
                continue
            write_array_diff(v, reference_dict[k], os.path.join(target_dir, k))

    def report_pickle_difference(self, new_output, reference):
        """Report the inconsistency of two files.

        To be called when output comparison assert fails.

        :param new_output: path to the new output file
        :param reference: path to the reference file
        :return: string message reporting the content of the new output
        """
        diff_fn = new_output + ".diff"
        diff_fd = open(diff_fn, "w")

        with open(new_output, "rb") as left:
            with open(reference, "rb") as right:
                new_output_dict = pickle.load(left, encoding="bytes")
                reference_dict = pickle.load(right, encoding="bytes")

                if not _is_on_github_action():
                    self._extract_pickle_data(
                        new_output_dict, self._extract_target_dir(new_output)
                    )
                    self._extract_pickle_data(
                        reference_dict, self._extract_target_dir(reference)
                    )
                    self._compare_arrays(
                        new_output_dict,
                        reference_dict,
                        self._extract_target_dir(new_output)
                    )

                new_output_str = self._data_to_str(new_output_dict)
                reference_str = self._data_to_str(reference_dict)

                # sys.stdout.writelines(
                #   unified_diff(new_output_str, reference_str)
                # )
                diff_fd.writelines(unified_diff(new_output_str, reference_str))

        diff_fd.close()

        return (
            "Inconsistency in {} compared to the reference data. "
            "The diff can be seen above and is stored in {}.".format(
                new_output, diff_fn
            )
        )

    def _run(self, comptype=None):
        runner = self.runner()
        Logger.setLevel(logging.ERROR)
        if self._params:
            runner.set_options(self._params)
        if comptype is not None:
            runner.workflow_mode = comptype

        runner.run()

    def run_dpre(self):
        self._run(WorkflowMode.dpre)

        dataprep_filepath = os.path.join(self._output_dir, "dpre.save")
        reference_filepath = os.path.join(
            self._output_dir,
            "..",
            "reference",
            "gistest_{}".format(self.reference_dir),
            "dpre",
            "dpre.save",
        )

        relative_tolerance = 0.0001

        with open(dataprep_filepath, "rb") as new_output:
            with open(reference_filepath, "rb") as reference:
                new_output_dict = pickle.load(new_output, encoding="bytes")
                reference_dict = pickle.load(reference, encoding="bytes")
                for k, v in new_output_dict.items():
                    if isinstance(v, dict):
                        for kk, vv in v.items():
                            if isinstance(vv[0], str):
                                equal = vv == reference_dict[k][kk]
                            else:
                                equal = np.allclose(
                                    vv, reference_dict[k][kk],
                                    rtol=relative_tolerance
                                )
                            assert equal is True, \
                                self.report_pickle_difference(
                                    dataprep_filepath, reference_filepath
                                )
                    elif v is None:
                        assert reference_dict[k] is None, \
                            self.report_pickle_difference(
                                dataprep_filepath, reference_filepath
                            )
                    else:
                        if k != 'rc':
                            equal = np.allclose(
                                v, reference_dict[k], rtol=relative_tolerance
                            )
                        else:
                            # cannot create an array from inhomogeneous list
                            equal = v == reference_dict[k]
                        assert equal, \
                            self.report_pickle_difference(
                                dataprep_filepath, reference_filepath
                            )

    def run_roff(self, config_file):
        assert os.path.exists(config_file)

        config = configparser.ConfigParser()
        config.read(config_file)

        os.environ["SMODERP2D_CONFIG_FILE"] = str(config_file)
        self._run()

        assert os.path.isdir(self._output_dir)

        testcase = os.path.splitext(os.path.basename(config_file))[0]
        if self.reference_dir is None:
            self.reference_dir = os.path.join(os.path.dirname(__file__),
                                              "data", "reference", testcase)
        if testcase == "gistest":
            self.reference_dir = os.path.join(self.reference_dir, "full")

        assert are_dir_trees_equal(
            self._output_dir, self.reference_dir
        )

    def run_full(self):
        self._run(WorkflowMode.full)

        assert os.path.isdir(self._output_dir)

        assert are_dir_trees_equal(
            self._output_dir,
            os.path.join(
                data_dir, "reference",
                "gistest_{}".format(self.reference_dir), "full"
            ),
        )
