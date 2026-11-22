from viktor.core import ViktorController


class Project(ViktorController):
    label = 'Project'
    children = ['Scenario']
    show_children_as = 'Table'
