import yaml
from math import cos, pi
import makegraphitics as mg

config = yaml.load(open("config.yaml"), Loader=yaml.FullLoader)
forcefield = "OPLS"

x_length = 20
y_length = 20

# calculate array of unit cells to make sheet
# unit cell is the orthorombic unit cell of graphene
unit_cell_x = 2.0 * config[forcefield]["CC"] * cos(pi / 6.0)
unit_cell_y = 3.0 * config[forcefield]["CC"]
x_cells = int(x_length / unit_cell_x)
y_cells = int(y_length / unit_cell_y)
layout = [x_cells, y_cells, 1]  # make an array of unit cells with this dimension

motif = mg.molecules.Graphene()
graphene = mg.Crystal(motif, layout)
vdw_defs = {1: 90}

mg.Parameterise(graphene, vdw_defs)

name = "graphene"
output = mg.Writer(graphene, name)
output.write_xyz(name + ".xyz")
output.write_lammps(name + ".data.lmp")
