from viktor.core import ViktorController
from viktor.views import Summary


class ProjectsFolder(ViktorController):
    label = 'Projects and tenders'
    summary = Summary()
    children = ['Project']
    show_children_as = 'Table'
