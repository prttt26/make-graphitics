import numpy as np
from oxidise_rf import init_random_forest
from base import Reactor


class Oxidiser(Reactor):
    def __init__(
        self,
        ratio=2.5,  # Target overall C/O ratio
        surface_OHratio=0.5,  # Surface OH/epoxy fraction
        edge_OHratio=0.25,  # edge H:OH:carboxyl ratio
        edge_carboxyl_ratio=0.25,  # edge H:OH:carboxyl ratio
        carboxyl_charged_ratio=0, # proportion of deprotonated carboxyls
        counterion=None, # Include counterion with charged carboxyl groups
        method="rf",  # which method to calculate a site's affinity
        # empirical / rf (random forest)
        new_island_freq=0,  # Freq s-1 attempt to add new island
        n_partitions=None,  # find_site speed up, for flakes > 100 nm
        video_xyz=False,
        video_lammps=False,
        stats=False,
    ):

        assert type(ratio) in [int, float] and ratio > 0
        self.target_ratio = ratio

        assert method in ["rf", "empirical"]
        self.method = method
        if self.method == "rf":
            # Read in data from Yang2014, generate random forest regressor
            self.rf = init_random_forest()

        assert type(new_island_freq) in [int, float]
        self.new_island_freq = new_island_freq

        assert (
            (n_partitions is None)
            or (n_partitions is False)  # default, will estimate a good partition
            or (type(n_partitions) == int and n_partitions > 0)  # no partitioning
        )  # specify
        self.n_partitions = n_partitions

        assert video_xyz == False or (type(video_xyz) == int and video_xyz > 0)
        self.video_xyz = video_xyz
        assert video_lammps == False or (type(video_lammps) == int and video_lammps > 0)
        self.video_lammps = video_lammps
        assert type(stats) == bool
        self.stats = stats

        assert 0 <= surface_OHratio <= 1
        assert 0 <= edge_OHratio <= 1
        assert 0 <= edge_carboxyl_ratio <= 1
        assert 0 <= carboxyl_charged_ratio <= 1
        assert edge_OHratio + edge_carboxyl_ratio <= 1

        assert counterion in [None, 'Na', 'Ca']
        if (counterion is not None) and (carboxyl_charged_ratio == 0):
            print("WARNING: you have specified a counterion without " +
                  "specifying carboxyl_charged_ratio, no counter ions " +
                  "will included")

        self.surface_OHratio = surface_OHratio
        self.edge_OHratio = edge_OHratio
        self.edge_carboxyl_ratio = edge_carboxyl_ratio
        self.carboxyl_charged_ratio = carboxyl_charged_ratio
        self.counterion = counterion

    def react(self, sim):
        # check sim is suitible for oxidation reaction implemented here
        self.validate_system(sim)

        # initialise data structures and reactivity information
        self.prepare_system(sim)

        self.Ncarbons = np.sum(np.array(sim.atom_labels) == 1)
        self.Nhydrogens = np.sum(np.array(sim.atom_labels) == 2)
        self.Noxygens = 0

        if self.Nhydrogens:
            self.oxidise_edges(sim)

        self.oxidise(sim)

        sim.generate_connections()
        return sim

    def validate_system(self, sim):
        # Check that this sim has only graphitic carbons and hydrogens
        assert set(np.unique(sim.atom_labels)).issubset({1, 2})

    def prepare_system(self, sim):
        sim.bond_graph = sim.generate_bond_graph(sim.bonds)

        (
            self.CCbonds,
            self.neighbours,
            self.CCbonds_next_to_atom,
        ) = self.neighbour_matrix(sim)
        self.NCCbonds = len(self.CCbonds)

        self.affinities_above, self.affinities_below = self.init_affinity_matrix(sim)
        self.atom_states = self.init_atom_states(sim)

        # lists to record oxidisation process
        self.time_order = []
        self.time_elapsed_list = []
        self.node_order = []

        self.partitions = self.set_partitions(self.n_partitions, self.NCCbonds)

        # all OPLS atom types that are introduced by oxidation
        sim.vdw_defs = {
            1: 90,  # Cg, graphitic (aromatic)
            2: 91,  # Hg, graphitic edge
            3: 101,  # Ct, tertiary C-OH
            4: 96,  # Oa, C-OH
            5: 97,  # Ha, C-OH
            6: 122,  # Oe, epoxy
            11: 108,  # Cb, Benzyl
            7: 109,  # Oa, C-OH
            8: 209,  # Cc, Carboxylic carbon
            9: 210,  # Oc, Carboxylic Ketone oxygen 
            10: 211,  # Oa, Carboxylic -OH 
            12: 213, # C, carboxylate -COO
            13: 214, # O, carboxylate -COO
            349: 349, # Na+
            354: 354, # Ca 2+ 
        }  # OPLS definitions

    def set_partitions(self, n_partitions, NCCbonds):
        if n_partitions is None:
            n_partitions = int(NCCbonds / 10000)
            if n_partitions == 0:
                n_partitions = 1
        elif n_partitions is False:
            n_partitions = 1
        self.n_partitions = n_partitions

        partition_size = int(NCCbonds / n_partitions)
        partitions = np.empty((n_partitions, 2), dtype=int)
        for i in range(n_partitions):
            partitions[i, 0] = i * partition_size
            partitions[i, 1] = (i + 1) * partition_size
        # in case NCCBonds is not evenly divisible,
        # include remainder in the last partition
        partitions[-1][1] = NCCbonds
        return partitions

    def oxidise_edges(self, sim):
        print "Oxidising edges"
        edge_OH = 0
        carboxyl = 0
        charge_carboxyl_sites = []
        charged_carboxyls = 0
        n_counterions = 0

        for i in range(len(sim.atom_labels)):
            if sim.atom_labels[i] == 2:
                r = np.random.random()
                if r < self.edge_OHratio:
                    self.add_edge_OH(sim, i)
                    self.Noxygens += 1
                    edge_OH += 1
                elif r > 1 - self.edge_carboxyl_ratio:
                    r2 = np.random.random()
                    if r2 > self.carboxyl_charged_ratio:
                        self.add_carboxyl(sim, i)
                    else:
                        charge_carboxyl_sites += [i]
                    self.Noxygens += 2
                    self.Ncarbons += 1
                    carboxyl += 1
                else:
                    pass  # leave as H

        # Ca counterions are 2+ charge
        # if odd number in charge_carboxyl_sites remove one
        if ((len(charge_carboxyl_sites) % 2) and
                (self.counterion == 'Ca')):
            self.add_carboxyl(sim, charge_carboxyl_sites[0])
            charge_carboxyl_sites = charge_carboxyl_sites[1:]

        for i, site in enumerate(charge_carboxyl_sites):
            counterion = self.counterion
            # Ca is a 2+ ion, add every other ion
            if counterion == "Ca" and i % 2:
                counterion = None
            if counterion:
                n_counterions += 1
            self.add_charged_carboxyl(sim, site, counterion)
            charged_carboxyls += 1

        print "added:"
        print edge_OH, "\tOH"
        print carboxyl - charged_carboxyls, "\tCOOH"
        print charged_carboxyls,"\tCOO-"
        if self.counterion:
            print n_counterions, self.counterion, "counterions"
        print "----------------------"
        print

        return edge_OH, carboxyl

    def oxidise(self, crystal):
        print "Oxidising basal plane"
        OH_added = 0
        epoxy_added = 0
        time_elapsed = 0  # since last new island
        dt = 0
        nodes = 0
        new_island = 1
        while self.ratio() > self.target_ratio:
            if not new_island:
                available_CC_bonds = np.sum(np.array(self.affinities_above != 0))
                new_island = np.random.poisson(
                    float(dt) * self.new_island_freq * available_CC_bonds
                )
            self.node_order += [new_island]
            self.time_elapsed_list += [time_elapsed]

            # choose site
            if new_island:
                dt = 0
                new_island -= 1
                time_elapsed = 0
                site, above, dt = self.find_site(crystal, new_island=True)
                nodes += 1
                print "new_island accepted,", nodes, "nodes (", new_island, ")"
            else:
                site, above, dt = self.find_site(crystal)
                time_elapsed += dt
            if above == 0:
                print "Could not reach C/O ratio:", self.target_ratio
                break

            # oxygenate at site,above
            r = np.random.random()  # between 0,1
            if r < self.surface_OHratio:
                # add OH
                r2 = np.random.randint(2)
                atom1 = self.CCbonds[site][r2] - 1
                self.add_OH(crystal, above, atom1)
                self.atom_states[atom1] = 1 * above
                self.update_affinity(atom1 + 1)
                OH_added += 1
            else:
                # add epoxy
                atom1, atom2 = self.CCbonds[site]
                atom1, atom2 = atom1 - 1, atom2 - 1
                self.add_epoxy(crystal, above, atom1, atom2)
                self.atom_states[atom1] = 2 * above
                self.atom_states[atom2] = 2 * above
                self.update_affinity(atom1 + 1)
                self.update_affinity(atom2 + 1)
                epoxy_added += 1
            self.Noxygens += 1

            # outputs
            if not self.Noxygens % 20:
                oxygens_to_add = int(self.Ncarbons / self.target_ratio)
                print self.Noxygens, "/", oxygens_to_add, "\toxygens added\t", nodes, "nodes"
            if self.video_xyz and not self.Noxygens % self.video_xyz:
                self.output_snapshot(crystal)
            if self.video_lammps and not self.Noxygens % self.video_lammps:
                self.output_snapshot(
                    crystal, format_="lammps", filename=str(self.Noxygens)
                )

        print OH_added, "\tOH were added"
        print epoxy_added, "\tepoxy were added"
        if epoxy_added != 0:
            print "OH/epoxy = ", float(OH_added) / (epoxy_added)
        else:
            print "OH/epoxy = inf"
        print nodes, "nodes"
        print "=========="
        print "C/O = ", self.Ncarbons, "/", self.Noxygens, "=", self.ratio()

    def ratio(self):
        if self.Noxygens == 0:
            ratio = float("inf")
        else:
            ratio = float(self.Ncarbons) / self.Noxygens
        return ratio

    def find_12_neighbours(self, crystal, i, j):
        expected_first_neighbours = 4
        expected_second_neighbours = 8

        first_neighbours = crystal.bonded_to(i - 1)
        first_neighbours += crystal.bonded_to(j - 1)
        first_neighbours = [n + 1 for n in first_neighbours]
        first_neighbours = set(first_neighbours) - {i, j}
        if len(first_neighbours) != expected_first_neighbours:
            raise ValueError("Not enough first neighbours", i, j, first_neighbours)

        second_neighbours = set()
        for atom in first_neighbours:
            if crystal.atom_labels[atom - 1] == 2:
                expected_second_neighbours -= 2
        for n in first_neighbours:
            second_neighbours = second_neighbours | set(crystal.bonded_to(n - 1))
        second_neighbours = {n + 1 for n in second_neighbours}
        second_neighbours = second_neighbours - first_neighbours - {i, j}
        if len(second_neighbours) != expected_second_neighbours:
            raise ValueError("Not enough second neighbours", i, j, second_neighbours)

        return list(first_neighbours) + list(second_neighbours)

    def init_affinity_matrix(self, crystal):
        affinities_above = np.ones(self.NCCbonds)
        affinities_below = np.ones(self.NCCbonds)
        for i in range(self.NCCbonds):
            first_neighbours = self.neighbours[i][0:4]
            for atom in first_neighbours:
                if crystal.atom_labels[atom - 1] == 2:
                    affinities_above[i] = 0
                    affinities_below[i] = 0
        return affinities_above, affinities_below

    def neighbour_matrix(self, crystal):
        Nbonds = len(crystal.bonds)
        CCbonds = []
        neighbours = []
        CCbonds_next_to_atom = {i + 1: set() for i in range(len(crystal.coords))}

        count = 0
        for i in range(Nbonds):
            c1 = crystal.bonds[i][0]
            c2 = crystal.bonds[i][1]
            label1 = crystal.atom_labels[c1 - 1]
            label2 = crystal.atom_labels[c2 - 1]

            if label1 == 1 and label2 == 1:
                CCbonds += [[c1, c2]]
                c1c2_neighbours = self.find_12_neighbours(crystal, c1, c2)
                neighbours += [c1c2_neighbours]

                CCbonds_next_to_atom[c1] |= {count}
                CCbonds_next_to_atom[c2] |= {count}
                for neighbour in c1c2_neighbours:
                    CCbonds_next_to_atom[neighbour] |= {count}
                count += 1

        return np.array(CCbonds), neighbours, CCbonds_next_to_atom

    def update_affinity(self, atom):
        for bond in self.CCbonds_next_to_atom[atom]:
            if atom in self.CCbonds[bond]:
                self.affinities_above[bond] = 0
                self.affinities_below[bond] = 0
            elif self.affinities_above[bond] != 0:
                self.calc_affinities(bond)

    def calc_affinities(self, site):
        calc_affinity = getattr(self, "calc_affinity_" + self.method)
        n = []
        for i in self.neighbours[site]:
            n += [self.atom_states[i - 1]]
        first = n[0:5]
        second = n[5:]
        above = calc_affinity(first, second)
        self.affinities_above[site] = above
        first = -np.array(first)
        second = -np.array(second)
        below = calc_affinity(first, second)
        self.affinities_below[site] = below

    def calc_affinity_rf(self, first, second):
        edge = False
        X = [0] * 8
        for state in first:
            if state == 1:
                X[0] += 1
            if state == -1:
                X[1] += 1
            if state == 2:
                X[2] += 1
            if state == -2:
                X[3] += 1
        for state in second:
            if state == 1:
                X[4] += 1
            if state == -1:
                X[5] += 1
            if state == 2:
                X[6] += 1
            if state == -2:
                X[7] += 1
            if state == 3:
                edge = True
        if edge:
            rate = 1
        else:
            exponent = self.rf.predict([X])
            rate = 10 ** exponent[0]
        return rate

    def calc_affinity_empirical(self, first, second):
        steric = 0
        polar = 0
        hbond = 0
        edge = 0
        m = [-3.867, 0.185, 23.169, -5.138, 11.648, -4.413]
        for state in first:
            if state == 1:
                steric += 1
            elif state == 2:
                steric += 1
            if abs(state) == 1:
                polar += 1
            if abs(state) == 2:
                polar += 0.633

            # if state == 1: hbond =

        for state in second:
            if state == 1:
                hbond += 1
            if state == 3:
                edge = 1

        steric = m[0] * steric + m[1] * steric * steric
        polar = m[2] * polar + m[3] * polar * polar
        hbond = m[4] * hbond + m[5] * hbond * hbond

        if edge:
            rate = 1
        else:
            rate = 10 ** (steric + polar + hbond)
        return rate

    def find_new_island(self):
        # number of sites that are not CH and can react
        bool_affinity = np.array(self.affinities_above != 0)
        total = np.sum(bool_affinity) * 2
        if total == 0:
            # no reactions possible
            return 0, 0

        r = np.random.random() * total
        R = 0
        above = 0
        for i, affinity in enumerate(bool_affinity):
            R += affinity
            if R > r:
                above = 1
                break
        if not above:
            for i, affinity in enumerate(bool_affinity):
                R += affinity
                if R > r:
                    above = -1
                    break
        if above == 0:
            # no possible oxidation sites
            raise Exception("Couldnt find a new island site")

        return i, above

    def find_site(self, sim, new_island=False):
        if new_island:
            reactivity_above = np.array(self.affinities_above != 0, dtype=float)
            reactivity_below = np.array(self.affinities_below != 0, dtype=float)
        else:
            reactivity_above = self.affinities_above
            reactivity_below = self.affinities_below

        totals_above = np.zeros(self.n_partitions)
        totals_below = np.zeros(self.n_partitions)
        for i in xrange(self.n_partitions):
            totals_above[i] = np.sum(
                reactivity_above[self.partitions[i][0] : self.partitions[i][1]]
            )
            totals_below[i] = np.sum(
                reactivity_below[self.partitions[i][0] : self.partitions[i][1]]
            )

        total_above = np.sum(totals_above)
        total_below = np.sum(totals_below)
        total = total_above + total_below
        if total == 0:
            # no reactions possible
            return 0, 0, 0

        r = np.random.random() * total

        def search(running_total, r, reactivity, totals):
            found = False
            for partition in range(self.n_partitions):
                running_total += totals[partition]
                if running_total > r:
                    running_total -= totals[partition]
                    for site in range(*self.partitions[partition]):
                        running_total += reactivity[site]
                        if running_total > r:
                            found = True
                            break
                    if found:
                        break
            assert found
            return site

        if r < total_above:
            site = search(0, r, reactivity_above, totals_above)
            above = 1
        else:
            site = search(total_above, r, reactivity_below, totals_below)
            above = -1

        # check its a valid site
        first_neighbours = self.neighbours[site][0:4]
        for atom in first_neighbours:
            if sim.atom_labels[atom - 1] == 2:
                raise Exception("i've picked an unallowed oxidation site...")

        if new_island:
            time = 0
        else:
            time = 1 / (total)
        self.time_order += [time]
        return site, above, time

    def add_edge_OH(self, crystal, H_at):
        bonded_to = crystal.bonded_to(H_at)
        C_at = bonded_to[0]
        if len(bonded_to) != 1:
            raise ValueError

        C_coord = crystal.coords[C_at]
        H_coord = crystal.coords[H_at]
        CO = 1.4
        OH = 0.7
        bond_vector = H_coord - C_coord
        bond_vector = bond_vector / np.linalg.norm(bond_vector)
        o_coord = C_coord + bond_vector * CO
        above = np.random.randint(2)
        h_coord = o_coord + bond_vector * OH + np.array(([0, 0, OH * (-1) ** above]))

        molecule = crystal.molecule_labels[C_at]
        O_at = H_at  # H becomes O to preserve bond already there
        H_at = len(crystal.atom_labels)
        crystal.atom_labels[C_at] = 11  # 3 is a C-OH carbon
        crystal.atom_charges[C_at] = 0.15
        crystal.coords[O_at] = o_coord
        crystal.atom_labels[O_at] = 7
        crystal.atom_charges[O_at] = -0.585

        crystal.coords = np.vstack((crystal.coords, h_coord))
        crystal.atom_labels += [5]  # H
        crystal.atom_charges += [0.435]
        crystal.molecule_labels += [molecule]

        new_bond = np.array(([O_at + 1, H_at + 1]))
        crystal.bonds = np.vstack((crystal.bonds, new_bond))

    def add_charged_carboxyl(self, crystal, H_at, counterion):
        bonded_to = crystal.bonded_to(H_at)
        C_at = bonded_to[0]
        if len(bonded_to) != 1:
            raise ValueError

        C_coord = crystal.coords[C_at]
        H_coord = crystal.coords[H_at]
        CC = 1.4
        CO = 1.4
        C_ion = 4.0
        angle = np.pi / 3
        sangle = np.sin(angle) * CO
        cangle = np.cos(angle) * CO
        bond_vector = H_coord - C_coord
        bond_vector = bond_vector / np.linalg.norm(bond_vector)

        C1_coord = C_coord + bond_vector * CC
        above = (-1) ** (np.random.randint(2))
        O1_coord = C1_coord + bond_vector * cangle + np.array([0, 0, sangle * above])
        O2_coord = O1_coord + np.array([0, 0, -2 * sangle * above])

        molecule = crystal.molecule_labels[C_at]

        C1_at = H_at
        O1_at = len(crystal.atom_labels)
        O2_at = O1_at + 1

        crystal.atom_labels[C_at] = 11
        crystal.atom_charges[C_at] = -0.100

        crystal.coords[C1_at] = C1_coord
        crystal.atom_labels[C1_at] = 12
        crystal.atom_charges[C1_at] = 0.7
        crystal.coords = np.vstack((crystal.coords, O1_coord))
        crystal.coords = np.vstack((crystal.coords, O2_coord))
        crystal.atom_labels += [13, 13]
        crystal.atom_charges += [-0.8, -0.8]
        crystal.molecule_labels += [molecule] * 2

        if counterion:
            counterion_coord = C1_coord + bond_vector * C_ion
            crystal.coords = np.vstack((crystal.coords, counterion_coord))
            crystal.molecule_labels += [ max(crystal.molecule_labels) + 1 ]
            if counterion == 'Na':
                crystal.atom_labels += [349]
                crystal.atom_charges += [+1.0]
            elif counterion == 'Ca':
                crystal.atom_labels += [354]
                crystal.atom_charges += [+2.0]
            else:
                raise Exception('Counterion not implemented:', counterion)


        new_bonds = np.array(
            ([C1_at + 1, O1_at + 1], [C1_at + 1, O2_at + 1])
        )
        crystal.bonds = np.vstack((crystal.bonds, new_bonds))


    def add_carboxyl(self, crystal, H_at):
        bonded_to = crystal.bonded_to(H_at)
        C_at = bonded_to[0]
        if len(bonded_to) != 1:
            raise ValueError

        C_coord = crystal.coords[C_at]
        H_coord = crystal.coords[H_at]
        CC = 1.4
        CO = 1.4
        OH = 1.1
        angle = np.pi / 3
        sangle = np.sin(angle) * CO
        cangle = np.cos(angle) * CO
        bond_vector = H_coord - C_coord
        bond_vector = bond_vector / np.linalg.norm(bond_vector)

        C1_coord = C_coord + bond_vector * CC
        above = (-1) ** (np.random.randint(2))
        O1_coord = C1_coord + bond_vector * cangle + np.array([0, 0, sangle * above])
        O2_coord = O1_coord + np.array([0, 0, -2 * sangle * above])

        H_coord = O2_coord + bond_vector * OH

        molecule = crystal.molecule_labels[C_at]

        C1_at = H_at
        O1_at = len(crystal.atom_labels)
        O2_at = O1_at + 1
        H_at = O1_at + 2

        crystal.atom_labels[C_at] = 11
        crystal.atom_charges[C_at] = -0.115

        crystal.coords[C1_at] = C1_coord
        crystal.atom_labels[C1_at] = 8
        crystal.atom_charges[C1_at] = 0.635
        crystal.coords = np.vstack((crystal.coords, O1_coord))
        crystal.coords = np.vstack((crystal.coords, O2_coord))
        crystal.coords = np.vstack((crystal.coords, H_coord))
        crystal.atom_labels += [9, 10, 5]
        crystal.atom_charges += [-0.44, -0.53, 0.45]
        crystal.molecule_labels += [molecule] * 3

        new_bonds = np.array(
            ([C1_at + 1, O1_at + 1], [C1_at + 1, O2_at + 1], [O2_at + 1, H_at + 1])
        )
        crystal.bonds = np.vstack((crystal.bonds, new_bonds))

    def add_OH(self, crystal, above, at):
        crystal.atom_labels[at] = 3  # 3 is a C-OH carbon
        crystal.atom_charges[at] = 0.265
        molecule = crystal.molecule_labels[at]

        CO = 1.4 * above
        OH = 1.0 * above
        o_coord = crystal.coords[at] + np.array([0, 0, CO])
        h_coord = o_coord + np.array([0, 0, OH])
        crystal.coords = np.vstack((crystal.coords, o_coord))
        crystal.coords = np.vstack((crystal.coords, h_coord))
        crystal.atom_labels += [4, 5]
        crystal.atom_charges += [-0.683, 0.418]
        crystal.molecule_labels += [molecule, molecule]

        hid = len(crystal.atom_labels)
        oid = hid - 1
        new_bonds = np.array(([at + 1, oid], [oid, hid]))
        crystal.bonds = np.vstack((crystal.bonds, new_bonds))

    def add_epoxy(self, crystal, above, c1, c2):
        crystal.atom_labels[c1] = 3  # 3 is epoxy carbon
        crystal.atom_labels[c2] = 3  # 3 is epoxy carbon
        crystal.atom_charges[c1] = 0.2
        crystal.atom_charges[c2] = 0.2

        molecule = crystal.molecule_labels[c1]

        c1c2 = crystal.coords[c2] - crystal.coords[c1]
        if c1c2[0] > 2:
            c1c2[0] += crystal.box_dimensions[0, 1]
        elif c1c2[0] < -2:
            c1c2[0] += crystal.box_dimensions[0, 1]
        if c1c2[1] > 2:
            c1c2[1] += crystal.box_dimensions[1, 1]
        elif c1c2[1] < -2:
            c1c2[1] += crystal.box_dimensions[1, 1]
        CO = 0.9 * above
        o_coord = crystal.coords[c1] + np.array([0, 0, CO]) + 0.5 * c1c2

        crystal.coords = np.vstack((crystal.coords, o_coord))
        crystal.atom_labels += [6]
        crystal.atom_charges += [-0.4]
        crystal.molecule_labels += [molecule]
        oid = len(crystal.atom_labels)
        new_bonds = np.array(([c1 + 1, oid], [c2 + 1, oid]))
        crystal.bonds = np.vstack((crystal.bonds, new_bonds))

    def remove_graphitic_bonds(self, crystal, a):
        connections = self.find_connections(crystal.bonds, a + 1)
        for i in range(len(connections)):
            bond = connections[i][0]
            if crystal.bond_labels[bond] == 1:
                crystal.bond_labels[bond] = 3

    def change_bond_label(self, crystal, a1, a2, label):
        connections = self.find_connections(crystal.bonds, a1 + 1)
        for i in range(len(connections)):
            bond = connections[i][0]
            if a2 + 1 in crystal.bonds[bond]:
                crystal.bond_labels[bond] = label

    def find_connections(self, bonds, centre):
        connections = np.where(bonds == centre)
        connections = np.vstack((connections[0], connections[1]))
        return connections.transpose()

    def oxidise_islands(self, crystal):
        removed = 1  # not 0
        sp3 = [3]
        epoxy_added = 0
        OH_added = 0
        while removed != 0:
            epoxy_added_cycle = 0
            OH_added_cycle = 0
            removed = 0
            for bond in crystal.bonds:
                c1 = bond[0] - 1
                c2 = bond[1] - 1
                label1 = crystal.atom_labels[c1]
                label2 = crystal.atom_labels[c2]
                # if i is a graphitic bond
                if label1 == 1 and label2 == 1:
                    # is it near oxidised sections
                    bonded_to = crystal.bonded_to(c1) + crystal.bonded_to(c2)
                    sp3_flag = 0
                    for atom in bonded_to:
                        if crystal.atom_labels[atom] in sp3:
                            sp3_flag += 1

                    # if bond is surrounded by sp3 carbons
                    if sp3_flag >= 3:
                        self.add_epoxy(crystal, c1, c2)
                        removed += 1
                        epoxy_added_cycle += 1

            for i in range(len(crystal.atom_labels)):
                if crystal.atom_labels[i] == 1:
                    bonded_to = crystal.bonded_to(i)
                    sp3_flag = 0
                    for atom in bonded_to:
                        if crystal.atom_labels[atom] in sp3:
                            sp3_flag += 1
                    # if bond is surrounded by sp3 carbons
                    if sp3_flag >= 3:
                        self.add_OH(crystal, i)
                        removed += 1
                        OH_added_cycle += 1

            print "cycle: islands removed", removed, " with ", epoxy_added_cycle, " epoxy and ", OH_added_cycle, " OH"
            OH_added += OH_added_cycle
            epoxy_added += epoxy_added_cycle

        return OH_added, epoxy_added

    def init_atom_states(self, sim):
        atom_states = np.zeros(len(sim.atom_labels))
        atom_states[atom_states == 2] = 3
        return atom_states
