from .projects_folder.controller import ProjectsFolder
from .project.controller import Project
from .scenario.controller import Scenario

from viktor import InitialEntity

initial_entities = [
    InitialEntity('ProjectsFolder', name='Projects')
]
