import numpy as np
from scipy.integrate import solve_ivp
import matplotlib.pyplot as plt

# Make sure to set up a virtual env with necessary libraries (np, matplotlib, etc.)
from pydrake.solvers import MathematicalProgram, OsqpSolver
import pydrake.symbolic as sym


from .init_quad_constants import Quad_Constants
from .MPC_controller import MPC, U_to_omega
from .LPV import LPV
from .full_dynamics import full_dynamics
from .trajectory_reference import trajectory_reference, plot_ref_trajectory
from .position_controller import feedback_linearization

from scripts.create_animation import Animation

# Quadrotor Configuration:
# 
#      1:CW    2:CCW
#         \     /
#          \___/
#          /   \
#         /     \
#      4:CCW    3:CW
#

class Flight_Controller(object):
        
    def time_conversion(self, t0, tf, constants):
        t = np.linspace(t0, tf, (int)(tf / (constants.Ts * constants.inner_loop)))
        return t
    
    def mpc_controller(self, t, t0, tf, traj_ref, xyz, constants):
        '''
        Inner and outer loop:
        - Outer loop calculates phi_ref, theta_ref abd U1 based on feedback_linearization (at a lower frequency)
        - Inner loop calculates U2, U3, U4 based on the MPC and the previous feedback_linearization
        '''
        Ts = constants.Ts
        controlled_states = constants.controlled_states # number of controlled states in this script
        inner_loop = constants.inner_loop # number of inner loop iterations
        hz = constants.hz # horizon period
        integral_steps = 20 # 80 is optimal here but takes longer

        total_inner_loop = (len(t) * inner_loop)
        dt = 1 / total_inner_loop
        
        print(f"t: {len(t)}, dt: {dt}, total_inner_loop: {total_inner_loop}")
        t_angles = np.arange(0, t[-1] + Ts, Ts)
        
        (X_ref, X_dot_ref, X_dot_dot_ref, 
        Y_ref, Y_dot_ref, Y_dot_dot_ref, 
        Z_ref, Z_dot_ref, Z_dot_dot_ref, 
        psi_ref) = traj_ref
        
        outer_loop = len(t) # Number of outer control loop iterations

        # Initial states
        zeros = [0] * 11  # Creates a list of 11 zeros
        ut, vt, wt, pt, qt, rt, xt, yt, zt, phit, thetat = zeros
        xt, yt, zt = xyz[0], xyz[1], xyz[2]

        
        psit = psi_ref[0,1]

        states = np.array([ut, vt, wt, pt, qt, rt, xt, yt, zt, phit, thetat, psit])
        states_total = np.array([states])

        # Initial angles reference
        ref_angles_total = [[phit, thetat, psit]]
        velocityXYZ_total = [[X_dot_ref[0, 1], Y_dot_ref[0, 1], Z_dot_ref[0, 1]]]

        # Initial drone state (rotors)
        omega1 = 110 * np.pi / 3 # rad/s at t = -1
        omega2 = 110 * np.pi / 3 # rad/s at t = -1
        omega3 = 110 * np.pi / 3 # rad/s at t = -1
        omega4 = 110 * np.pi / 3 # rad/s at t = -1

        ct = constants.ct
        cq = constants.cq
        l  = constants.l

        U1 = ct *  (omega1**2 + omega2**2 + omega3**2 + omega4**2)
        U2 = ct * l * (omega2**2 - omega4**2)
        U3 = ct * l * (omega3**2 - omega1**2)
        U4 = cq * (-omega1**2 + omega2**2 - omega3**2 + omega4**2)

        UTotal = [[U1,U2,U3,U4]]

        # Define global variable omega_total (used in LPV)
        omega_total = omega1 - omega2 + omega3 - omega4

        # Outer control loop
        for i in range(outer_loop - 1):
            
            # Compute remaining time to avoid overshooting        
            remaining_time = tf - i * (tf / len(t))
            print(f"Iteration: {i+1}/{outer_loop - 1}, remaining time in trajectory: {np.round(remaining_time)}s ", end="\r")
            
            dt = min(dt, remaining_time)  # Adjust dt dynamically
            
            # Position controller
            phi_ref, theta_ref, U1 = feedback_linearization(
                X_ref[i + 1, 1], X_dot_ref[i + 1, 1], X_dot_dot_ref[i + 1, 1],
                Y_ref[i + 1, 1], Y_dot_ref[i + 1, 1], Y_dot_dot_ref[i + 1, 1],
                Z_ref[i + 1, 1], Z_dot_ref[i + 1, 1], Z_dot_dot_ref[i + 1, 1],
                psi_ref[i + 1, 1], states, constants
            )

            Phi_ref = np.ones(inner_loop + 1) * phi_ref
            Theta_ref = np.ones(inner_loop + 1) * theta_ref

            Psi_ref = np.zeros(inner_loop + 1)
            for yaw_step in range(inner_loop + 1):
                Psi_ref[yaw_step] = psi_ref[i,1] + (psi_ref[i + 1, 1]-psi_ref[i,1])/(Ts*inner_loop)*Ts*(yaw_step)

            for ang_row in range(1, len(Phi_ref)):
                ref_angles_total.append([Phi_ref[ang_row], Theta_ref[ang_row], Psi_ref[ang_row]])

            # Create the reference vector for inner loop
            refSignals = np.zeros((len(Phi_ref) * controlled_states, 1))
            k_ref_local = 0
            for idx in range(len(Phi_ref)):
                refSignals[k_ref_local] = Phi_ref[idx]
                refSignals[k_ref_local + 1] = Theta_ref[idx]
                refSignals[k_ref_local + 2] = Psi_ref[idx]
                k_ref_local += controlled_states

            # Inner control loop
            k_ref_local = 0
            current_hz = hz
            for _ in range(inner_loop):
                Ad, Bd, Cd, x_dot, y_dot, z_dot, phit, phi_dot, thetat, theta_dot, psit, psi_dot = LPV(constants, states, omega_total)
                velocityXYZ_total.append([x_dot, y_dot, z_dot])

                x_aug_t = np.array([phit, phi_dot, thetat, theta_dot, psit, psi_dot, U2, U3, U4])

                k_ref_local += controlled_states
                if k_ref_local + controlled_states * current_hz - 1 <= len(refSignals):
                    r = refSignals[k_ref_local : k_ref_local+controlled_states*current_hz]
                else:
                    r = refSignals[k_ref_local:]
                    current_hz = len(r)//controlled_states

                # Compute MPC simplification matrices
                Hdb, Fdbt,_,_ = MPC(constants, Ad, Bd, Cd, current_hz)

                # Set up QP (using OSQP via Drake)
                prog = MathematicalProgram()
                du = prog.NewContinuousVariables(current_hz * Bd.shape[1], "du")

                # Cost: 0.5*du^T Hdb du + ft^T du
                x_aug_t_r = np.concatenate((x_aug_t, r.flatten()))
                ft = x_aug_t_r @ Fdbt
                
                # Add quadratic cost: Hdb must be symmetric positive semidefinite
                # Cost: 0.5 * du^T Hdb du + ft du
                # Drake's AddQuadraticCost takes Q, b, c with cost = (1/2) x'Qx + b'x + c
                # prog.AddQuadraticCost(0.5*du.T @ Hdb @ du + ft @ du) less efficient
                prog.AddQuadraticCost(Q=Hdb, b=ft, c=0, vars=du)

                solver = OsqpSolver()
                result = solver.Solve(prog)
                
                if result.is_success():
                    du_sol = result.GetSolution(du)
                else:
                    print("QP solver failed.")
                    du_sol = np.zeros_like(du)

                # Update inputs
                U2 += du_sol[0]
                U3 += du_sol[1]
                U4 += du_sol[2]

                UTotal.append([U1,U2,U3,U4])

                # Compute new omegas
                omega1, omega2, omega3, omega4 = U_to_omega(np.array([U1,U2,U3,U4]), constants.M_inv)

                omega_total = omega1 - omega2 + omega3 - omega4
                
                sub_dt = dt
                states_current = states.copy()
                
                for _ in range(integral_steps):
                    # Integrate one small step from 0 to sub_dt with first_step=sub_dt
                    def f(t_local, zeta_local):
                        return full_dynamics().continuous(constants, zeta_local, U1, U2, U3, U4, omega_total)
                    
                    # This integrates a single step of size sub_dt
                    sol = solve_ivp(f, (0, sub_dt), states_current, first_step=sub_dt, max_step=sub_dt)
                    
                    # Update states to final of this sub-step
                    states_current = sol.y[:, -1]
                
                states = states_current
                states_total = np.vstack((states_total, states[np.newaxis, :]))

        print("MPC Simulation finished.")
        # Example: print final states
        print("Final states:", states_total[-1, 6:9])
        
        return states_total, t0, tf

    def plot_mpc_results_3d(self, constants, zeta, t0, tf, ax_tpv, ax_non_tpv, name,
                            t_ref=0, x=None, y=None, z=None
    ):
        """
        Plots the trajectory and control inputs of the 3D quadrotor.
        """
        
        '''
        # Define design parameters
        D2R = np.pi/180
        R2D = 180/np.pi
        b = 0.6 # length of total square cover by whole quad body
        a = b/3 # length of  a small square base of quad (b/4)
        H = 0.06 # height of drone in z direction
        H_m = H + H/2 # height of motors in Z direction
        r_p = b/4

        ro = 45*D2R
        Ri = np.array([
            [np.cos(ro), -np.sin(ro), 0],
            [np.sin(ro),  np.cos(ro), 0],
            [0,           0,          1]
        ])
        base_co = np.array([[-a/2, a/2,  a/2, -a/2],
                            [-a/2,-a/2,  a/2,  a/2],
                            [   0,   0,    0,    0]])
        base = Ri @ base_co
        to = np.linspace(0, 2*np.pi)
        xp = r_p*np.cos(to)
        yp = r_p*np.sin(to)
        zp = np.zeros(len(to))
        '''
        
        
        # TRAJECTORY
        for ax in [ax_tpv]:
            # staes are: [ut, vt, wt, pt, qt, rt, xt, yt, zt, phit, thetat, psit]
            ax.plot(zeta[:, 6], zeta[:, 7], zeta[:, 8], 'r--', lw=1, label="Trajectory")
            ax.scatter(zeta[0, 6], zeta[0, 7], zeta[0, 8], color='red', label="Start")
            # print(f"Start: {zeta[0, 6], zeta[0, 7], zeta[0, 8]}")
            ax.scatter(zeta[-1, 6], zeta[-1, 7], zeta[-1, 8], color='green', label="End")
            # print(f"End: {zeta[-1, 6], zeta[-1, 7], zeta[-1, 8]}")
            
            plot_ref_trajectory(constants, t_ref, ax, x=x, y=y, z=z)
            
        for ax in [ax_non_tpv]:   
            X_ref,_,_,Y_ref,_,_,Z_ref,_,_,_ = trajectory_reference(constants, t_ref, x=x, y=y, z=z)

            # Extract trajectory data
            x_vals = X_ref[:,1]
            y_vals = Y_ref[:,1]
            z_vals = Z_ref[:,1]
            
            ax.plot(x_vals, y_vals, label="Reference Trajectory", color="blue")
            
        t_ref = np.linspace(t0, tf, len(zeta[:,0]))     

        # # ANGLES
        # plt.figure()
        # plt.plot(t[1:], zeta[1:, 3], label="Phi")
        # plt.plot(t[1:], zeta[1:, 4], label="Theta")
        # plt.plot(t[1:], zeta[1:, 5], label="Psi")
        # plt.xlabel("Time (s)")
        # plt.ylabel("Angles (rad)")
        # plt.title(f"Angles over time ({name})")
        # plt.legend()
        # # plt.show()
        # plt.savefig(f"{name}_angles.png")
        # print(f"Saved angles plot as {name}_angles.png")

        # plt.figure()
        # plt.plot(t[1:], omega[1:])
        # plt.xlabel("Time (s)")
        # plt.ylabel("Control Inputs (rad/s)")
        # plt.title(f"Control Inputs ({name})")
        # plt.legend([f"omega{i+1}" for i in range(omega.shape[1])])
        # # plt.show()
        # plt.savefig(f"{name}_inputs.png")
        # print(f"Saved input plot as {name}_inputs.png")

        return t_ref

def fcn():
    constants = Quad_Constants()
    controller = Flight_Controller()
    # Generate reference signals
    t0 = 0
    tf = 100
    
    t = controller.time_conversion(t0, tf, constants)
    traj_ref = trajectory_reference(constants, t)
    
    states_total, t0, tf = controller.mpc_controller(t, traj_ref, constants)
    x_limits, y_limits, z_limits, t = controller.plot_mpc_results_3d(constants, states_total, t0, tf, "3d_Quad")
    
    # Animate the quadrotor
    Animation().animate_quadrotor(constants, states_total, t, x_limits, y_limits, z_limits)

if __name__ == "__main__":
    # fcn()
    constants = Quad_Constants()
    controller = Flight_Controller()
    # Generate reference signals
    t0 = 0
    tf = 100
    
    t = controller.time_conversion(t0, tf, constants)
    traj_ref = trajectory_reference(constants, t)
    
    states_total, t0, tf = controller.mpc_controller(t, traj_ref, [0,0,0], constants)
    x_limits, y_limits, z_limits, t = controller.plot_mpc_results_3d(constants, states_total, t0, tf, "3d_Quad")
    
    # Animate the quadrotor
    Animation().animate_quadrotor(constants, states_total, t, x_limits, y_limits, z_limits)