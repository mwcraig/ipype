import logging
from pathlib import Path
import zipfile
from zipfile import is_zipfile
import shutil
import io
import copy

import traitlets
from traitlets.config import Configurable, Application
from traitlets.config.loader import PyFileConfigLoader, JSONFileConfigLoader, KeyValueConfigLoader, KVArgParseConfigLoader
import nbformat
from nbconvert.exporters import Exporter

from ipype.notebook import export_notebook, execute_notebook, notebook_to_html, \
get_notebooks_in_zip, extract_notebook_from_zip, ZipFileTuple, is_valid_notebook

from ipype.preprocessors import IPypeExecutePreprocessor


class ZippedPipelineConfigLoader(PyFileConfigLoader):
    def _read_file_as_dict(self):
        
        def get_config():
            return self.config
        
        zip_file = self.full_filename
        zip_file_pth = Path(zip_file)
        
        with zipfile.ZipFile(zip_file, 'r') as zipped:
            #try python config first
            try:
                zipinfo = zipped.getinfo("config.py")
                config_type = 'python'
            except KeyError:
                try:
                    zipinfo = zipped.getinfo("config.json")
                    config_type = 'json'
                except KeyError:
                    return #return silently
                        
            if config_type == 'python':
                with zipped.open(zipinfo, 'r') as f:
                    namespace = dict(
                    c=self.config,
                    load_subconfig=self.load_subconfig,
                    get_config=get_config,
                    __file__=self.full_filename,
                    )
                    exec(compile(f.read(), zip_file_pth.name, 'exec'), namespace)
            else:
                with zipped.open(zipinfo, 'r') as f:
                    return json.load(f)

class DirPipelineConfigLoader(PyFileConfigLoader):
    def __new__(self, dir_name_pth):
        dir_name_pth = Path(dir_name_pth)
        
        if (dir_name_pth / 'config.py').exists():
            return PyFileConfigLoader(str(dir_name_pth / 'config.py'))
        elif (dir_name_pth / 'config.json').exists():
            return JSONFileConfigLoader(str(dir_name_pth / 'config.json'))
        else:
            return PyFileConfigLoader(str(dir_name_pth / 'config.py'))
            

#class Pipeline(Configurable):
class Pipeline(Exporter):
    requires = traitlets.List()
    path = traitlets.Unicode().tag(config=True)
    output_dir = traitlets.Unicode().tag(config=True)
    extra_args = traitlets.Tuple().tag(config=True)
    notebook_pattern = traitlets.Unicode("*.ipynb")
    
    output_subdirs = traitlets.List(['data','exec_notebooks','html','logs','pipeline', 'results','tmp'])
    
    _preprocessors = traitlets.List(['ipype.preprocessors.IPypeExecutePreprocessor'])

    def initialize(self):
        self._path = Path(self.path).absolute()
        self._output = Path(self.output_dir).absolute()

        if self._path.is_dir():
            self._notebooks = self._path.glob(self.notebook_pattern)
        elif is_zipfile(str(self._path)):
            self._notebooks = get_notebooks_in_zip(str(self._path))
        elif self._path.is_file():
            if is_valid_notebook(str(self._path)):
                self._notebooks = [self._path] # list with one notebook
            else:
                raise Exception("Could not validate notebook")

        self.init_preprocessor()
        
        self.init_configloader()
        
        print(self.config)
        
        x
        
    def init_preprocessor(self):
        preprocessor = IPypeExecutePreprocessor(timeout=-1)
        preprocessor.log = self.parent.log
        self.preprocessor = preprocessor
    
    def init_configloader(self):
        if self._path.is_dir():
            self.configloader = DirPipelineConfigLoader(str(self._path))
            self.config.merge(self.configloader.load_config())
        elif is_zipfile(str(self._path)):
            self.configloader = ZippedPipelineConfigLoader(str(self._path))
            self.config.merge(self.configloader.load_config())
        else:
            pass
        
        print(self.extra_args)
        cmdline_args_config = KVArgParseConfigLoader(self.extra_args).load_config()
        print(cmdline_args_config)
        self.config.merge(cmdline_args_config)
        
        
    def _output_subdir(self, subdir):
        return (self._output / subdir)
    
    def _make_output_dir(self):
        if not self._output.exists():
            self._output.mkdir()

    def _make_output_subdirs(self):
        for subdir in self.output_subdirs:
            subdir_pth = self._output / subdir
            subdir_pth.mkdir()
    
    def _setup_logging(self):
        try:
            self.logger = self.log = self.parent.log
        except:
            print("error setting up logging")
            self.logger = self.log = logging.getLogger(__name__)
        
        self.logger.setLevel(logging.INFO)

        log_file_handler = logging.FileHandler(str(self._output / 'pipeline.log'))
        log_file_handler.setLevel(logging.INFO)
        self.logger.addHandler(log_file_handler)

        timestamp_log_handler = logging.FileHandler(str(self._output_subdir('logs') / 'timestamps.log'))
        timestamp_formatter = logging.Formatter('%(asctime)s - %(message)s')
        timestamp_log_handler.setFormatter(timestamp_formatter)
        timestamp_log_handler.setLevel(logging.DEBUG)
        self.logger.addHandler(timestamp_log_handler)
        
    
    def init_notebooks(self):
        #copy "unexecuted" notebooks (to pipeline subdir)
        self.extracted_notebooks = []
        
        for notebook_file in self._notebooks:
            if isinstance(notebook_file, ZipFileTuple):
                zipfiletuple = notebook_file
                extract_notebook_from_zip(str(zipfiletuple.zipfile_path), zipfiletuple.member_info.filename, self._output_subdir('pipeline'))
                dst_notebook_pth = self._output_subdir('pipeline') / zipfiletuple.member_info.filename
                self.extracted_notebooks.append(dst_notebook_pth)

            elif isinstance(notebook_file, Path):
                dst_notebook_pth = self._output_subdir('pipeline') / notebook_file.name
                shutil.copy(str(notebook_file), str(dst_notebook_pth))
                self.extracted_notebooks.append(dst_notebook_pth)
                

        #list and set the notebooks (all extracted notebooks)
        #############
        #self.notebooks = self._output_subdir('pipeline').glob(self.notebook_pattern)
        self.notebooks = self.extracted_notebooks
        #TODO:decide which is the better line to set the notebooks
        

    
    def export_single_notebook(self, notebook_filename, resources=None, input_buffer=None):
        notebook_filename = Path(notebook_filename)
        notebook_exec_name = notebook_filename.with_suffix('.exec.ipynb').name
        notebook_exec_pth = self._output_subdir('exec_notebooks') / notebook_exec_name
        nb, resources = export_notebook(notebook_filename, self.preprocessor, metadata_path_str=str(self._output))
        return nb, resources
        
    def convert_single_notebook(self, notebook_filename, input_buffer=None):
        
        notebook_filename = Path(notebook_filename)
        notebook_exec_name = notebook_filename.with_suffix('.exec.ipynb').name
        notebook_exec_pth = self._output_subdir('exec_notebooks') / notebook_exec_name
        
        self.logger.info("Starting to execute {}".format(str(notebook_filename)))
        nb, resources = self.export_single_notebook(notebook_filename)
        self.logger.info("Finished executing {}".format(str(notebook_filename)))
    

        with io.open(str(notebook_exec_pth), 'wt', encoding='utf-8') as f:
            nbformat.write(nb, f)
        
        self.exec_notebooks.append(notebook_exec_pth)
    
    
    def _convert_executed_notebooks_to_html(self, executed_notebooks):
        for exec_notebook in self.exec_notebooks:
            html_notebook_name = exec_notebook.name.split('.exec.ipynb')[0] + ".html"
            html_notebook_pth = self._output_subdir('html') / html_notebook_name
            notebook_to_html(exec_notebook, html_notebook_pth)
    
    def convert_notebooks(self):
        self.exec_notebooks = []
        
        for notebook_filename in self.notebooks:
            self.convert_single_notebook(notebook_filename)
        
        #export notebooks (to html)
        self._convert_executed_notebooks_to_html(self.exec_notebooks)
        
    def run(self):
        output_path = self._output
   
        #make sure output directory exists
        self._make_output_dir()
        
        #make subdirs
        self._make_output_subdirs()

        #setup logging
        self._setup_logging()        
        
        #copy "unexecuted" notebooks (to pipeline subdir)
        self.init_notebooks()
        
        #execute notebooks
        self.convert_notebooks()
        
    
    def start(self):
        """Run start after initialization process has completed"""
        self.run()
    
    
    
class IPypeApp(Application):
    name = 'ipype'
    description = "IPype Application"
    
    classes = traitlets.List([Pipeline])
    
    aliases = {'pipeline': 'Pipeline.path',
               'output': 'Pipeline.output_dir'}
    
    flags = {}
    
    def init_pipeline(self):
         self.pipeline = Pipeline(config=self.config, parent=self)
         self.pipeline.initialize()
         
    def initialize(self):
        #if self.config_file: self.load_config_file(self.config_file)
        self.init_pipeline()
        
             