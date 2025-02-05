# Phillip's model
# Direct translation of fortran code, using fortran arrays starting at one.

# The time integration is carried forward using the vorticity in a three
# time level scheme. At each step the streamfunction must be diagnosed from
# this.

# The streamfunction is denoted by s and vorticity by v.
# Vector wind components are uc and vc.
# Total fields are denoted with suffix t and zonal means with suffix z

import numpy as np
from scipy.linalg.lapack import dgtsv, dgetrf, dgetrs
import matplotlib.pyplot as plt
import matplotlib.animation as animation
import numba as nb
import time
import netCDF4

def msq_rand(x):
    # Middle 10 digits following Hammer
    return ((x*x) // 10**5) % 10**10

class Grid:
    L = 6.0e6
    W = 5.0e6  # y coord goes from -W to W (Phillips notation])
    nx = 16
    ny = 16
    dx = L / nx   # 3.75e5
    dy = 2*W / ny # 6.25e5    # Gridsize

class Var():

    # Level 1 and 3 components of 3D variable

    def __init__(self):
        # Total field
        self.l1t = np.zeros( (Grid.nx+1,Grid.ny+1) )
        self.l3t = np.zeros( (Grid.nx+1,Grid.ny+1) )
        # Anomaly (zonal mean removed)
        self.l1  = np.zeros( (Grid.nx+1,Grid.ny+1) )
        self.l3  = np.zeros( (Grid.nx+1,Grid.ny+1) )
        # Zonal means
        self.l1z  = np.zeros( Grid.ny+1 )
        self.l3z  = np.zeros( Grid.ny+1 )

    def dump(self):
        for j in range(0,Grid.ny+1):
            for i in range(1,Grid.nx+1):
                print(f"{self.l1t[i,j]:12.4f}", end="")
            print()

    def adump(self):
        for j in range(0,Grid.ny+1):
            for i in range(1,Grid.nx+1):
                print(f"{self.l1[i,j]:12.4f}", end="")
            print()

    def settot(self,val):
        if isinstance(val,Var):
            self.l1t[:] = val.l1t
            self.l3t[:] = val.l3t
        else:
            self.l1t[:] = val
            self.l3t[:] = val

    def set(self,val):
        if isinstance(val,Var):
            self.l1[:] = val.l1
            self.l3[:] = val.l3
        else:
            self.l1[:] = val
            self.l3[:] = val

    def calc_zmean(self):
        self.l1z[:] = self.l1t[1:Grid.nx+1,:].mean(axis=0)
        self.l3z[:] = self.l3t[1:Grid.nx+1,:].mean(axis=0)

    def split(self):
        self.calc_zmean()
        self.l1[:] = self.l1t[:] - self.l1z[:]
        self.l3[:] = self.l3t[:] - self.l3z[:]


class Model:

    eps = Grid.dx/Grid.dy
    epsq = eps*eps
    heat = 2.0e-3           #  Heating W/kg
    lambdasq = 1.5e-12      # Stability parameter, m^-2
    rgas = 287.0
    cp = 1004.0
    f0 = 1.0e-4
    beta = 1.6e-11          # s^{-1} m^{-1}
    p4 = 1.0e5              # Surface pressure, Pa
    p2 = 0.5*p4
    gamma = lambdasq*Grid.dx**2  # 0.21
    k = 4.0e-6              # s^{-1}  Surface drag

    # Control variables
    a = 1.0e5               # m^2/s   Horizonal diffusion
    diag_flag = False
    accel = 1.0

    noisescale = 7.509e6

    # For netcdf output
    save_netcdf = False

    day1 = 131.0  # Zonal spin up length
    dt1 = 86400.  # Spin up time step
    day2 = 165.5    # Total run length
    dt2 = 7200.   # Initial time step in regular run
    dt = dt1
    min_dt = 1800.
    variable_step = True
    diag_freq = 3600

    first_step = True # For solver initialisation

    # All initialised to zero, so model is at rest
    v =  Var() #  ! Vorticity
    vm = Var() #  ! Vorticity at tau - 1 values
    s  = Var() #  ! Streamfunction
    # Temporaries used in timestepping
    x = Var()

    time = 0
    day = 0
    np.seterr(over='raise', invalid='raise', divide='raise')

    ps_offset = 1040
    ps_levels = np.arange(-75,-6,5) + ps_offset
    ps_cmap = 'jet'
    T_levels = np.linspace(-30,30,16)
    T_cmap = 'RdBu_r'
    # u_levels = np.linspace(-70,70,15)   # 250 hPa
    u_levels = np.linspace(-15,15,11)   # 1000 hPa
    # u_levels = np.linspace(-25,25,11)   # 750 hPa
    u_cmap = 'RdBu_r'


    def calcvor(self, s, v):
        # This repeats same code for each level. Should the level be another dimension?
        for j in range(1,Grid.ny):
            jm = j-1
            jp = j+1
            for i in range(1,Grid.nx+1):
                im = i-1
                if im == 0:
                    im = Grid.nx
                ip = i+1
                if ip == Grid.nx+1:
                    ip = 1
                v.l1t[i,j] = ( s.l1t[ip,j]  + s.l1t[im,j] - 2*s.l1t[i,j] ) + \
                    self.epsq * ( s.l1t[i,jp] + s.l1t[i,jm] - 2*s.l1t[i,j] ) - \
                    self.gamma * ( s.l1t[i,j] - s.l3t[i,j] )
                v.l3t[i,j] = ( s.l3t[ip,j] + s.l3t[im,j] - 2*s.l3t[i,j] ) + \
                    self.epsq * ( s.l3t[i,jp] + s.l3t[i,jm] - 2*s.l3t[i,j] ) + \
                    self.gamma * ( s.l1t[i,j] - s.l3t[i,j] )
        # Follow A17 and set end rows to zonal mean of neighbours
        v.l1t[:,0] = v.l1t[:,1].mean()
        v.l1t[:,Grid.ny] = v.l1t[:,Grid.ny-1].mean()
        v.l3t[:,0] = v.l3t[:,1].mean()
        v.l3t[:,Grid.ny] = v.l3t[:,Grid.ny-1].mean()

    def calc_zonstream(self, v, s):
        # Given vorticity variable as input, solve for the
        # zonal mean streamfunction

        ny = Grid.ny
        nz = 2*ny - 3  #  Number of zonal means to solve for
        epsq = self.epsq
        gamma = self.gamma

        if self.first_step:
            self.first_step = False
            # Start these arrays from 1 again
            amat = np.zeros((nz+1,nz+1))

            # j = 1, level 1
            amat[1,1] = -epsq - gamma
            amat[1,2] = epsq
            # j = J-1, level 1
            amat[ny-1,ny-2] = epsq
            amat[ny-1,ny-1] = -epsq - gamma
            amat[ny-1,nz] = gamma
            # j = 2, level 3
            amat[ny,2] = gamma
            amat[ny,ny] = -2.0*epsq - gamma
            amat[ny,ny+1] = epsq
            # j = J-1, level 3
            amat[nz,ny-1] = gamma
            amat[nz,nz-1] = epsq
            amat[nz,nz] = -epsq - gamma

            #  Level 1
            for j in range(2,ny-1):
                amat[j,j-1] = epsq
                amat[j,j] = -2.0*epsq - gamma
                amat[j,j+1] = epsq
                amat[j,ny-2+j] = gamma

            #  Level 3
            for j in range(ny+1, nz):
                amat[j,j+2-ny] = gamma
                amat[j,j-1] = epsq
                amat[j,j] = -2.0*epsq - gamma
                amat[j,j+1] = epsq

            self.lu, self.piv, info = dgetrf(amat[1:,1:])

        bmat = np.zeros(nz+1)
        bmat[1:ny] = v.l1z[1:ny]
        for j in range(2,ny):
            bmat[ny-2+j] = v.l3z[j]

        # Solve AX=B
        bmat[1:], info = dgetrs(self.lu, self.piv, bmat[1:])

        for j in range(1,ny):
            s.l1z[j] = bmat[j]
        for j in range(2,ny):
            s.l3z[j] = bmat[ny-2+j]

        # Apply the BC
        s.l1z[0] = s.l1z[1]
        s.l1z[ny] = s.l1z[ny-1]
        s.l3z[0] = 0.0
        # Note the extra restriction on s3(1) which is not solved for.
        s.l3z[1] = 0.0
        s.l3z[ny] = s.l3z[ny-1]

    def relax1(self, v, s):
        # Solve for anomaly streamfunction

        # print("V1 anom", abs(v.l1).max())
        nx = Grid.nx
        ny = Grid.ny
        # Start from the current value of the anomaly streamfunction
        for iter in range(100):
            # Jacobi iteration
            maxdiff = 0.0
            change = 0.0
            # for irb in range(2):
            for j in range(1,ny):
                jm = j-1
                jp = j+1
                for i in range(1,nx+1):
                    im = i-1
                    if im == 0:
                        im = nx
                    ip = i+1
                    if ip == nx+1:
                        ip = 1

                    resid = ( s.l1[ip,j] + s.l1[im,j] +
                                self.epsq*( s.l1[i,jp] + s.l1[i,jm] ) -
                                v.l1[i,j] + self.gamma*s.l3[i,j] ) -  \
                                ( 2.0 + 2.0*self.epsq + self.gamma )*s.l1[i,j]
                    resid = self.accel*resid / ( 2.0 + 2.0*self.epsq + self.gamma )
                    change = change + resid**2
                    maxdiff = max ( maxdiff, abs(resid) )
                    s.l1[i,j] = s.l1[i,j] + resid

                    resid = ( s.l3[ip,j] + s.l3[im,j] +
                                self.epsq*( s.l3[i,jp] + s.l3[i,jm] ) -
                                v.l3[i,j] + self.gamma*s.l1[i,j] ) -  \
                                ( 2.0 + 2.0*self.epsq + self.gamma )*s.l3[i,j]

                    resid = self.accel*resid / ( 2.0 + 2.0*self.epsq + self.gamma )
                    change = change + resid**2
                    maxdiff = max ( maxdiff, abs(resid) )
                    s.l3[i,j] = s.l3[i,j] + resid
            # print("ITER1", iter, np.sqrt(change), maxdiff)
            # maxdiff is now only on a single level so halve the convergence
            # criterion
            if maxdiff < 0.5*3.75e4:
                # print("ITER1 done", iter, np.sqrt(change), maxdiff)
                break
        if iter >= 100:
            raise Exception(f"RELAX1 failed {iter} {np.sqrt(change)} {maxdiff}")

    def relax2(self, x, dt, v):
        # Solve for anomaly vorticity

        temp = Var()
        nx = Grid.nx
        ny = Grid.ny

        v.l1[:] = 0.0
        v.l3[:] = 0.0
        alpha = self.a*dt/Grid.dx**2
        for iter in range(100):
            # Jacobi iteration
            for j in range(1,ny):
                jm = j-1
                jp = j+1
                for i in range(1,nx+1):
                    im = i-1
                    if im == 0:
                        im = nx
                    ip = i+1
                    if ip == nx+1:
                        ip = 1
                    temp.l1[i,j] = ( alpha*( v.l1[ip,j] + v.l1[im,j] +
                                             self.epsq * ( v.l1[i,jp] + v.l1[i,jm] ) ) +
                                     x.l1[i,j]  ) /    \
                                     ( 2*alpha*(1.0 + self.epsq)  + 1.0 )
                    temp.l3[i,j] = ( alpha*( v.l3[ip,j] + v.l3[im,j] +
                                             self.epsq * ( v.l3[i,jp] + v.l3[i,jm] ) ) +
                                     x.l3[i,j]  ) /    \
                                     ( 2*alpha*(1.0 + self.epsq)  + 1.0 +1.5*self.k*dt )
            change1 = np.sum ( ( v.l1[1:,1:] - temp.l1[1:,1:] ) **2 )
            v.l1[:,1:ny] = temp.l1[:,1:ny]
            # Boundary condition A17
            v.l1[:,0] = v.l1[:,1].mean()
            v.l1[:,ny] = v.l1[:,ny-1].mean()
            change3 = np.sum ( ( v.l3[1:,1:] - temp.l3[1:,1:] ) **2 )
            v.l3[:,1:ny] = temp.l3[:,1:ny]
            v.l3[:,0] = v.l3[:,1].mean()
            v.l3[:,ny] = v.l3[:,ny-1].mean()
            if max(change1, change3) < 1.0:
                # print("ITER2", iter, np.sqrt(change1), np.sqrt(change3))
                break

    def xcalc(self, v, vm, s, dt, x):

        nx = Grid.nx
        ny = Grid.ny
        alpha = self.a*dt/Grid.dx**2
        b = self.beta*Grid.dx**2*Grid.dy
        c = dt/(2.0*Grid.dx*Grid.dy)
        h = 4*self.rgas*self.heat*self.gamma*dt/(self.f0*self.cp)
        x.l1t[:] = 0.0
        x.l3t[:] = 0.0
        for j in range(1,ny):
            jm = j-1
            jp = j+1
            for i in range(1,nx+1):
                im = i-1
                if im == 0:
                    im = nx
                ip = i+1
                if ip == nx+1:
                    ip = 1

                x.l1t[i,j] = vm.l1t[i,j] +                                           \
                    c * ( (v.l1t[ip,j]-v.l1t[im,j])*(s.l1t[i,jp]-s.l1t[i,jm]) -             \
                          (2*b+v.l1t[i,jp]-v.l1t[i,jm])*(s.l1t[ip,j]-s.l1t[im,j]) ) +      \
                          alpha * ( vm.l1t[ip,j]+vm.l1t[im,j]-2*vm.l1t[i,j] +           \
                                    self.epsq*(vm.l1t[i,jp]+vm.l1t[i,jm]-2*vm.l1t[i,j]) ) +        \
                        h*(2*j-ny)/ny

                x.l3t[i,j] = vm.l3t[i,j] +                                             \
                    c * ( (v.l3t[ip,j]-v.l3t[im,j])*(s.l3t[i,jp]-s.l3t[i,jm]) -              \
                           (2*b+v.l3t[i,jp]-v.l3t[i,jm])*(s.l3t[ip,j]-s.l3t[im,j]) ) +       \
                           alpha * ( vm.l3t[ip,j]+vm.l3t[im,j]-2*vm.l3t[i,j] +            \
                                    self.epsq*(vm.l3t[i,jp]+vm.l3t[i,jm]-2*vm.l3t[i,j]) ) -  \
                        h*(2*j-ny)/ny
                x.l3t[i,j] = x.l3t[i,j] -  self.k*dt*(1.5*vm.l3t[i,j] - v.l1t[i,j] -
                                    4*self.gamma*(s.l1t[i,j]-s.l3t[i,j]) )

    def calc_zvor(self, x, dt, v):
        # Solve for the zonal mean vorticity
        ny = Grid.ny
        epsq = self.epsq

        nz = ny-1
        amat = np.zeros(nz-1)
        bmat = np.zeros(nz)
        cmat = np.zeros(nz-1)
        rmat = np.zeros(nz)

        alpha = self.a*dt/Grid.dx**2
        # Level 1
        amat[:] = alpha*epsq
        bmat[0] = bmat[nz-1] = -alpha*epsq - 1
        bmat[1:nz-1] = -2.0*alpha*epsq - 1
        cmat[:] = alpha*epsq
        rmat[:] = -x.l1z[1:nz+1]

        result = dgtsv(amat, bmat, cmat, rmat)
        umat = result[3]
        v.l1z[1:nz+1] = umat[:]
        v.l1z[0] = v.l1z[1]
        v.l1z[ny] = v.l1z[ny-1]

        # Level 3
        # amat[:] = alpha*epsq
        # bmat[0] = bmat[nz-1] = -alpha*epsq - 1 - 1.5*self.k*dt
        # bmat[1:nz-1] = -2.0*alpha*epsq - 1 - 1.5*self.k*dt
        # cmat[:] = alpha*epsq
        bmat -= 1.5*self.k*dt
        rmat[:] = -x.l3z[1:nz+1]

        result = dgtsv(amat, bmat, cmat, rmat)
        umat = result[3]
        v.l3z[1:nz+1] = umat[:]
        v.l3z[0] = v.l3z[1]
        v.l3z[ny] = v.l3z[ny-1]

    def zonal_diag(self, day, s):
        u = Var()
        ny = Grid.ny
        dy = Grid.dy

        t2z = self.f0*(s.l1z[:]-s.l3z[:])/self.rgas
        #  Zonal wind, u = -pd{psi}{y}
        u.l1z[1:] = - ( s.l1z[1:ny+1] - s.l1z[0:ny] ) / dy
        u.l3z[1:] = - ( s.l3z[1:ny+1] - s.l3z[0:ny] ) / dy

        # Zonal KE, scaled such that a wind of 1 m/s everywhere gives 10
        zke = 10.0*np.sum(u.l1z*u.l1z + u.l3z*u.l3z)/(2*ny)

        # Zonal PE  A21
        # Sum 1:J-1 / J matches definition of Y operator
        zpe = 5*self.lambdasq*np.sum((s.l1z[1:ny] - s.l3z[1:ny])**2) / ny

        print("TZ %5.1f %15.7f %15.7f %15.7f %15.7f %15.7f" % ( day, t2z.max(), u.l1z.max(), u.l3z.max(), zke, zpe))

    def calc_T(self):
        return self.f0*(self.s.l1t-self.s.l3t)/self.rgas

    def calc_ps(self):
        return 0.01 * (1.5*self.s.l3t - 0.5*self.s.l1t)*self.f0

    def calc_u(self, s):
        u = Var()
        u.l1t[:,1:] = - ( s.l1t[:,1:Grid.ny+1] - s.l1t[:,0:Grid.ny] ) / Grid.dy
        u.l3t[:,1:] = - ( s.l3t[:,1:Grid.ny+1] - s.l3t[:,0:Grid.ny] ) / Grid.dy
        return u

    def calc_v(self, s):
        v = Var()
        for i in range(1,Grid.nx+1):
            im = i-1
            if im == 0:
                im = Grid.nx
            v.l1t[i,:] = ( s.l1t[i,:] - s.l1t[im,:] ) / Grid.dx
            v.l3t[i,:] = ( s.l3t[i,:] - s.l3t[im,:] ) / Grid.dx
        return v

    def calc_energy(self, s, u, v):
        # Calculate zonal mean and eddy winds
        u.split()
        v.split()
        nx = Grid.nx; ny = Grid.ny

        zke = 10.0*np.sum(u.l1z*u.l1z + u.l3z*u.l3z)/(2*ny)
        eke = 10.0*np.sum(u.l1**2 + u.l3**2 + v.l1**2 + v.l3**2)/(2*ny*nx)

        zpe = 5*self.lambdasq*np.sum((s.l1z[1:ny] - s.l3z[1:ny])**2) / ny
        epe = 5*self.lambdasq*np.sum((s.l1[:,1:ny] - s.l3[:,1:ny])**2) / (nx*ny)

        return zke, eke, zpe, epe

    def diag(self, day, s):

        nx = Grid.nx; ny = Grid.ny
        dx = Grid.dx; dy = Grid.dy

        u = self.calc_u(s)
        v = self.calc_v(s)
        zke, eke, zpe, epe = self.calc_energy(s, u, v)

        # Is this KE with the shifted winds useful?
        # Which is best in the energy conversions?
        # vshift = Var()
        # #  Average v to get it on the same grid points as u
        # for i in range(1,nx+1):
        #     ip = i+1
        #     if ip > nx:
        #         ip = 1
        #     for j in range(1,ny+1):
        #         vshift.l1t[i][j] = 0.25*(v.l1t[i][j] + v.l1t[ip][j] +
        #                             v.l1t[i][j-1] + v.l1t[ip][j-1])
        #         vshift.l3t[i][j] = 0.25*(v.l3t[i][j] + v.l3t[ip][j] +
        #                             v.l3t[i][j-1] + v.l3t[ip][j-1])
        # # print("MAX V", v.l1t.max(), vshift.l1t.max())
        # vshift.split()
        # # Note factor of 10 here.
        # tke = 10.0*np.sum(u.l1t[1:,1:]*u.l1t[1:,1:] + u.l3t[1:,1:]*u.l3t[1:,1:] +
        #                     vshift.l1t[1:,1:]*vshift.l1t[1:,1:] + vshift.l3t[1:,1:]*vshift.l3t[1:,1:])/(2*ny*nx)

        print("KE %6.2f %9.2f %9.2f %9.2f %9.2f" %( day, zke, eke, epe, zpe))
        if eke > 1e5:
            raise Exception("EKE too large")

    def stability_criterion(self, dt, s):
        # Stability criterion (A13)
        smax = 0.
        for i in range(1,Grid.nx+1):
            im = i-1
            if im == 0:
                im = Grid.nx
            ip = i+1
            if ip > Grid.nx:
                ip = 1
            for j in range(1,Grid.ny):
                jm = j-1
                jp = j+1
                smax = max(smax, abs(s.l1t[ip,j]-s.l1t[im,j]) + abs(s.l1t[i,jp]-s.l1t[i,jm]))
                smax = max(smax, abs(s.l3t[ip,j]-s.l3t[im,j]) + abs(s.l3t[i,jp]-s.l3t[i,jm]))
        return 0.5*dt*smax / (Grid.dx*Grid.dy)

    def create_nc_output(self):

        self.ds = netCDF4.Dataset('c:/Users/dix043/temp/phillips_model.nc', 'w')
        ds = self.ds
        ds.createDimension('lon', Grid.nx)
        ds.createDimension('lat', 1+Grid.ny)
        ds.createDimension('vlon', Grid.nx)
        ds.createDimension('ulat', Grid.ny)
        ds.createDimension('lev', 2)
        ds.createDimension('time', None)
        v = ds.createVariable('lon', np.float32, ('lon',))
        v.long_name = 'longitude'
        v.units = 'degrees_east'
        v = ds.createVariable('lat', np.float32, ('lat',))
        v.long_name = 'latitude'
        v.units = 'degrees_north'
        v = ds.createVariable('vlon', np.float32, ('vlon',))
        v.long_name = 'V longitude'
        v.units = 'degrees_east'
        v = ds.createVariable('ulat', np.float32, ('ulat',))
        v.long_name = 'U latitude'
        v.units = 'degrees_north'
        v = ds.createVariable('lev', np.float32, ('lev',))
        v.long_name = 'model level'
        v = ds.createVariable('time', np.float32, ('time',))
        v.units = "days since 2000-01-01 00:00"
        v = ds.createVariable('strm', np.float32, ('time', 'lev', 'lat', 'lon'))
        v.long_name = 'streamfunction'
        v.units = 'm2 s-1'
        v = ds.createVariable('vor', np.float32, ('time', 'lev', 'lat', 'lon'))
        v.long_name = 'vorticity'
        v.units = 's-1'
        v = ds.createVariable('t500', np.float32, ('time', 'lat', 'lon'))
        v.long_name = 'air temperature at 500 hPa'
        v.units = 'K'
        v = ds.createVariable('ps', np.float32, ('time', 'lat', 'lon'))
        v.long_name = 'surface pressure'
        v.units = 'hPa'
        v = ds.createVariable('u', np.float32, ('time', 'lev', 'ulat', 'lon'))
        v.long_name = 'zonal wind'
        v.units = 'm s-1'
        v = ds.createVariable('v', np.float32, ('time', 'lev', 'lat', 'vlon'))
        v.long_name = 'meridional wind'
        v.units = 'm s-1'
        v = ds.createVariable('eke', np.float32, ('time',))
        v.long_name = "Eddy kinetic energy (*10)"
        v = ds.createVariable('zke', np.float32, ('time',))
        v.long_name = "Zonal kinetic energy (*10)"
        v = ds.createVariable('epe', np.float32, ('time',))
        v.long_name = "Eddy potential energy (*10)"
        v = ds.createVariable('zpe', np.float32, ('time',))
        v.long_name = "Zonal potential energy (*10)"

        # At latitude 45
        dlon = 360 * Grid.dx / (6.371e6*2*np.pi*np.cos(np.pi/4))
        ds.variables['lon'][:] = np.arange(Grid.nx) * dlon
        lat0 = 45.
        dlat = 360 * Grid.dy / (6.371e6*2*np.pi)
        ds.variables['lat'][:] = 45 + dlat*np.arange(-Grid.ny//2, Grid.ny//2+1)
        ds.variables['vlon'][:] = ds.variables['lon'][:] - 0.5*dlon
        ds.variables['ulat'][:] = ds.variables['lat'][:-1] + 0.5*dlat
        ds.variables['lev'][:] = [1., 3.]

        self.irec = -1

    def nc_output(self, day, v, s):
        # Python variables are (nx, ny) so need to transpose when writing
        # to match netCDF dimensions
        self.irec += 1
        self.ds.variables['vor'][self.irec,0] = v.l1t[1:].T
        self.ds.variables['vor'][self.irec,1] = v.l3t[1:].T
        self.ds.variables['strm'][self.irec,0] = s.l1t[1:].T
        self.ds.variables['strm'][self.irec,1] = s.l3t[1:].T

        u = self.calc_u(s)
        self.ds.variables['u'][self.irec,0] = u.l1t[1:,1:].T
        self.ds.variables['u'][self.irec,1] = u.l3t[1:,1:].T
        vtmp = self.calc_v(s)
        self.ds.variables['v'][self.irec,0] = vtmp.l1t[1:].T
        self.ds.variables['v'][self.irec,1] = vtmp.l3t[1:].T

        zke, eke, zpe, epe = self.calc_energy(s, u, vtmp)
        self.ds.variables['zke'][self.irec] = zke
        self.ds.variables['eke'][self.irec] = eke
        self.ds.variables['zpe'][self.irec] = zpe
        self.ds.variables['epe'][self.irec] = epe

        self.ds.variables['t500'][self.irec] = self.calc_T()[1:].T
        self.ds.variables['ps'][self.irec] = self.calc_ps()[1:].T

        # Use time since perturbation
        self.ds.variables['time'][self.irec] = day - self.day1

    def spinup(self):

        v = self.v
        vm = self.vm
        s = self.s
        x = self.x

        # Time loop
        while True:
            v.split()
            self.calc_zonstream(v, s)

            s.l1[:] = 0.0
            s.l3[:] = 0.0

            # Should be a function
            for j in range(Grid.ny+1):
                s.l1t[:,j] = s.l1[:,j] + s.l1z[j]
                s.l3t[:,j] = s.l3[:,j] + s.l3z[j]
            if self.time % self.diag_freq == 0:
                self.zonal_diag(self.day, s)

            # Time stepping
            # self.xcalc(v, vm, s, self.dt, x)
            alpha = self.a*self.dt/Grid.dx**2
            b = self.beta*Grid.dx**2*Grid.dy
            c = self.dt/(2.0*Grid.dx*Grid.dy)
            h = 4*self.rgas*self.heat*self.gamma*self.dt/(self.f0*self.cp)
            xcalc_nb(v.l1t, v.l3t, vm.l1t, vm.l3t, s.l1t, s.l3t, x.l1t, x.l3t,
                     self.dt, Grid.nx, Grid.ny, self.epsq, alpha, b, c, h, self.k, self.gamma)

            x.split()

            # Solve for new zonal mean vorticity from x
            self.calc_zvor(x, self.dt, v)

            if self.time == 0.0:
                # Forward step from rest. This simple form for the forward step
                # assumes that it's starting from rest.
                v.l1z = 0.5*x.l1z
                v.l3z = 0.5*x.l3z
            else:
                # Update previous value of vorticity
                vm.settot(v)

            v.set(0.0)
            for j in range(Grid.ny+1):
                v.l1t[:,j] = v.l1z[j]
                v.l3t[:,j] = v.l3z[j]

            self.time += self.dt
            self.day = self.time/86400.0
            if self.day >= self.day1:
                break

        return vm, v

    def perturb(self):

        vm = self.vm
        v = self.v
        stmp = Var() # For random initialisation
        vtmp = Var() # For random initialisation

        # Time step has changed so linearly interpolate the previous time
        # value to ensure a smooth start
        vm.l1t[:] = v.l1t - (v.l1t-vm.l1t)*self.dt2/self.dt1
        vm.l3t[:] = v.l3t - (v.l3t-vm.l3t)*self.dt2/self.dt1

        rval = 1_111_111_111
        for i in range(1,Grid.nx+1):
            for j in range(1,Grid.ny):
                rval = msq_rand(rval)
                stmp.l1t[i,j] = float(rval) / 10**10
                stmp.l3t[i,j] = stmp.l1t[i,j]
        # Remove the zonal mean and scale
        stmp.split()
        stmp.l1t[:] = self.noisescale*stmp.l1[:]
        stmp.l3t[:] = self.noisescale*stmp.l3[:]
        # Convert this to a vorticity anomaly
        self.calcvor(stmp, vtmp)
        v.l1t[:] += vtmp.l1t
        v.l3t[:] += vtmp.l3t
        vm.l1t[:] += vtmp.l1t
        vm.l3t[:] += vtmp.l3t

    def step(self):

        v = self.v
        vm = self.vm
        s = self.s
        x = self.x

        v.split()
        self.calc_zonstream(v, s)

        #  Use relaxation to solve for the anomaly streamfunction
        relax1_nb(v.l1, v.l3, s.l1, s.l3, Grid.nx, Grid.ny, self.epsq, self.gamma)


        # Should be a function
        for j in range(Grid.ny+1):
            s.l1t[:,j] = s.l1[:,j] + s.l1z[j]
            s.l3t[:,j] = s.l3[:,j] + s.l3z[j]
        if self.time % self.diag_freq == 0:
            self.diag(self.day, s)
            if self.save_netcdf:
                self.nc_output(self.day, v, s)

        # Time stepping
        # self.xcalc(v, vm, s, self.dt, x)
        alpha = self.a*self.dt/Grid.dx**2
        b = self.beta*Grid.dx**2*Grid.dy
        c = self.dt/(2.0*Grid.dx*Grid.dy)
        h = 4*self.rgas*self.heat*self.gamma*self.dt/(self.f0*self.cp)
        xcalc_nb(v.l1t, v.l3t, vm.l1t, vm.l3t, s.l1t, s.l3t, x.l1t, x.l3t,
                    self.dt, Grid.nx, Grid.ny, self.epsq, alpha, b, c, h, self.k, self.gamma)

        x.split()

        # Solve for new zonal mean vorticity from x
        self.calc_zvor(x,self.dt,v)

        if self.time == 0.0:
            # Forward step from rest. This simple form for the forward step
            # assumes that it's starting from rest.
            v.l1z = 0.5*x.l1z
            v.l3z = 0.5*x.l3z
        else:
            # Update previous value of vorticity
            vm.settot(v)

        # Relaxation solver for non-zonal terms
        # self.relax2(x, self.dt, v)
        alpha = self.a*self.dt/Grid.dx**2
        relax2_nb(v.l1, v.l3, x.l1, x.l3, self.dt, Grid.nx, Grid.ny, alpha, self.epsq, self.k)

        for j in range(Grid.ny+1):
            v.l1t[:,j] = v.l1[:,j] + v.l1z[j]
            v.l3t[:,j] = v.l3[:,j] + v.l3z[j]

        self.time += self.dt
        self.day = self.time/86400.0

        if self.variable_step and self.time % 86400 == 0 and self.dt > self.min_dt:
            stab_crit = self.stability_criterion(self.dt, s)
            # print(f"Stability  {self.day:.2f} {stab_crit:.3f}")
            if stab_crit > 0.9:
                print(f"At {self.day:.2f} {stab_crit:.3f} Adjusting time step to {self.dt-self.min_dt}")
                vm.l1t[:] = v.l1t - (v.l1t-vm.l1t)*(self.dt-self.min_dt)/self.dt
                vm.l3t[:] = v.l3t - (v.l3t-vm.l3t)*(self.dt-self.min_dt)/self.dt
                self.dt -= self.min_dt

@nb.jit(cache=True)
def relax1_nb(v1, v3, s1, s3, nx, ny, epsq, gamma):
    # Solve for anomaly streamfunction

    # Start from the current value of the anomaly streamfunction
    for iter in range(100):
        # Jacobi iteration
        maxdiff = 0.0
        change = 0.0
        # for irb in range(2):
        for j in range(1,ny):
            jm = j-1
            jp = j+1
            for i in range(1,nx+1):
                im = i-1
                if im == 0:
                    im = nx
                ip = i+1
                if ip == nx+1:
                    ip = 1

                resid = ( s1[ip,j] + s1[im,j] +
                            epsq*( s1[i,jp] + s1[i,jm] ) -
                            v1[i,j] + gamma*s3[i,j] ) -  \
                            ( 2.0 + 2.0*epsq + gamma )*s1[i,j]
                resid = resid / ( 2.0 + 2.0*epsq + gamma )
                change = change + resid**2
                maxdiff = max ( maxdiff, abs(resid) )
                s1[i,j] = s1[i,j] + resid

                resid = ( s3[ip,j] + s3[im,j] +
                            epsq*( s3[i,jp] + s3[i,jm] ) -
                            v3[i,j] + gamma*s1[i,j] ) -  \
                            ( 2.0 + 2.0*epsq + gamma )*s3[i,j]

                resid = resid / ( 2.0 + 2.0*epsq + gamma )
                change = change + resid**2
                maxdiff = max ( maxdiff, abs(resid) )
                s3[i,j] = s3[i,j] + resid
        # print("ITER1", iter, np.sqrt(change), maxdiff)
        # maxdiff is now only on a single level so halve the convergence
        # criterion
        if maxdiff < 0.5*3.75e4:
            # print("ITER1 done", iter, np.sqrt(change), maxdiff)
            break
    if iter >= 100:
        raise Exception(f"RELAX1 failed {iter} {np.sqrt(change)} {maxdiff}")

@nb.jit(cache=True)
def relax2_nb(v1, v3, x1, x3, dt, nx, ny, alpha, epsq, k):
    # Solve for anomaly vorticity

    temp1 = np.zeros( (nx+1,ny+1) )
    temp3 = np.zeros( (nx+1,ny+1) )

    v1[:] = 0.0
    v3[:] = 0.0
    for iter in range(100):
        # Jacobi iteration
        for j in range(1,ny):
            jm = j-1
            jp = j+1
            for i in range(1,nx+1):
                im = i-1
                if im == 0:
                    im = nx
                ip = i+1
                if ip == nx+1:
                    ip = 1
                temp1[i,j] = ( alpha*( v1[ip,j] + v1[im,j] +
                                            epsq * ( v1[i,jp] + v1[i,jm] ) ) +
                                    x1[i,j]  ) /    \
                                    ( 2*alpha*(1.0 + epsq)  + 1.0 )
                temp3[i,j] = ( alpha*( v3[ip,j] + v3[im,j] +
                                            epsq * ( v3[i,jp] + v3[i,jm] ) ) +
                                    x3[i,j]  ) /    \
                                    ( 2*alpha*(1.0 + epsq)  + 1.0 +1.5*k*dt )
        change1 = np.sum ( ( v1[1:,1:] - temp1[1:,1:] ) **2 )
        v1[:,1:ny] = temp1[:,1:ny]
        # Boundary condition A17
        v1[:,0] = v1[:,1].mean()
        v1[:,ny] = v1[:,ny-1].mean()
        change3 = np.sum ( ( v3[1:,1:] - temp3[1:,1:] ) **2 )
        v3[:,1:ny] = temp3[:,1:ny]
        v3[:,0] = v3[:,1].mean()
        v3[:,ny] = v3[:,ny-1].mean()
        if max(change1, change3) < 1.0:
            # print("ITER2", iter, np.sqrt(change1), np.sqrt(change3))
            break

@nb.jit(cache=True)
def xcalc_nb(v1t, v3t, vm1t, vm3t, s1t, s3t, x1t, x3t, dt, nx, ny, epsq, alpha, b, c, h, k, gamma):

    x1t[:] = 0.0
    x3t[:] = 0.0
    for j in range(1,ny):
        jm = j-1
        jp = j+1
        for i in range(1,nx+1):
            im = i-1
            if im == 0:
                im = nx
            ip = i+1
            if ip == nx+1:
                ip = 1

            x1t[i,j] = vm1t[i,j] +                                           \
                c * ( (v1t[ip,j]-v1t[im,j])*(s1t[i,jp]-s1t[i,jm]) -             \
                        (2*b+v1t[i,jp]-v1t[i,jm])*(s1t[ip,j]-s1t[im,j]) ) +      \
                        alpha * ( vm1t[ip,j]+vm1t[im,j]-2*vm1t[i,j] +           \
                                epsq*(vm1t[i,jp]+vm1t[i,jm]-2*vm1t[i,j]) ) +        \
                    h*(2*j-ny)/ny

            x3t[i,j] = vm3t[i,j] +                                             \
                c * ( (v3t[ip,j]-v3t[im,j])*(s3t[i,jp]-s3t[i,jm]) -              \
                        (2*b+v3t[i,jp]-v3t[i,jm])*(s3t[ip,j]-s3t[im,j]) ) +       \
                        alpha * ( vm3t[ip,j]+vm3t[im,j]-2*vm3t[i,j] +            \
                                epsq*(vm3t[i,jp]+vm3t[i,jm]-2*vm3t[i,j]) ) -  \
                    h*(2*j-ny)/ny
            x3t[i,j] = x3t[i,j] -  k*dt*(1.5*vm3t[i,j] - v1t[i,j] -
                                4*gamma*(s1t[i,j]-s3t[i,j]) )

class Animation():
    def __init__(self, m, t1):
        self.m = m
        self.t1 = t1
        fig = plt.figure(figsize=(12,8))
        self.axes1 = fig.add_subplot(1,3,1)
        self.p = plt.contourf(self.m.calc_ps().T[::-1,1:]+self.m.ps_offset, levels=self.m.ps_levels, cmap=self.m.ps_cmap, extend='both')
        self.pc = plt.contour(self.m.calc_ps().T[::-1,1:]+self.m.ps_offset, levels=self.m.ps_levels, colors='black', negative_linestyles='solid')
        plt.colorbar(self.p, orientation='horizontal', label='Sea level pressure (hPa)')
        self.axes2 = fig.add_subplot(1,3,2)
        self.pT = plt.contourf(self.m.calc_T().T[::-1,1:], levels=self.m.T_levels, cmap=self.m.T_cmap, extend='both')
        plt.colorbar(self.pT, orientation='horizontal', label='Temperature at 500 hPa ($\degree$C)')
        self.axes3 = fig.add_subplot(1,3,3)
        u = self.m.calc_u(self.m.s)
        usurf = 1.5*u.l3t - 0.5*u.l1t
        self.pU = plt.contourf(usurf.T[::-1,1:], levels=self.m.u_levels, cmap=self.m.u_cmap, extend='both')
        # self.pU = plt.contourf(self.m.calc_u(self.m.s).l1t.T[::-1,1:], levels=self.m.u_levels, cmap=self.m.u_cmap, extend='both')
        plt.colorbar(self.pU, orientation='horizontal', label='Zonal wind at 1000 hPa (m/s)')
        self.animation = animation.FuncAnimation(fig, self.update, frames=10000,
                                interval=0, repeat=False)
        self.axes2.set_title(f"Day {self.m.day-self.m.day1:.2f}\n", fontsize=20)
        plt.tight_layout()


        fig.canvas.mpl_connect('button_press_event', self.toggle_pause)
        self.paused = False
        self.dosleep = False
        # self.paused=True
        # self.animation.pause()

    def toggle_pause(self, event):
        # If paused, use right button to single step
        if event.button == 1:
            if self.paused:
                self.animation.resume()
            else:
                self.animation.pause()
            self.paused = not self.paused
        if self.paused and event.button==3:
            self.animation._step()

    def update(self,i):

        if self.m.day > self.m.day2:
            self.animation.event_source.stop()
            t2 = time.perf_counter()
            print("Elapsed time", t2-self.t1)

        # This gives a chance to pause on the first frame
        if i > 3:
            self.m.step()

        # tmp = self.m.calc_ps().T[::-1,1:]
        # print(tmp.max(), tmp.min())
        # For animating a contour plot
        # https://scipython.com/blog/animated-contour-plots-with-matplotlib/
        self.p.remove()
        self.p = self.axes1.contourf(self.m.calc_ps().T[::-1,1:]+self.m.ps_offset, levels=self.m.ps_levels, cmap=self.m.ps_cmap, extend='both')
        self.pc.remove()
        self.pc = self.axes1.contour(self.m.calc_ps().T[::-1,1:]+self.m.ps_offset, levels=self.m.ps_levels, colors='black', negative_linestyles='solid')
        self.axes2.set_title(f"Day {self.m.day-self.m.day1:.2f}\n", fontsize=20)
        # For a pcolormesh simply reset the data
        # self.pT.set_array(self.m.calc_T().T[::-1,1:].flatten())
        self.pT.remove()
        self.pT = self.axes2.contourf(self.m.calc_T().T[::-1,1:], levels=self.m.T_levels, cmap=self.m.T_cmap, extend='both')
        self.pU.remove()
        u = self.m.calc_u(self.m.s)
        usurf = 1.5*u.l3t - 0.5*u.l1t
        self.pU = self.axes3.contourf(usurf.T[::-1,1:], levels=self.m.u_levels, cmap=self.m.u_cmap, extend='both')
        # self.pU = self.axes3.contourf(self.m.calc_u(self.m.s).l1t.T[::-1,1:], levels=self.m.u_levels, cmap=self.m.u_cmap, extend='both')
        plt.tight_layout()

def main():
    m = Model()
    m.spinup()
    if m.save_netcdf:
        m.create_nc_output()
        # Save unperturbed as a prior time
        m.nc_output(m.day1-m.dt2, m.v, m.s)
    m.perturb()
    m.dt = m.dt2
    animate = True
    t1 = time.perf_counter()
    if animate:
        anim = Animation(m, t1)
        plt.show()
    else:
        t1x = 0
        while m.day < m.day2:
            m.step()
            if not t1x:
                t1x = time.perf_counter()
        t2 = time.perf_counter()
        print("Elapsed time", t2-t1)
        print("Elapsed time excluding compilation", t2-t1x)

# import cProfile
# from pstats import SortKey
# cProfile.run('main()', sort=SortKey.CUMULATIVE)
main()
