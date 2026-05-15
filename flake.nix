{
  description = "Nothing Ear Linux companion app";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      supportedSystems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs supportedSystems;
    in
    {
      packages = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };
          python = pkgs.python3.withPackages (ps: with ps; [
            dbus-python
            pycairo
            pygobject3
          ]);
          giTypelibPath = pkgs.lib.makeSearchPath "lib/girepository-1.0" (with pkgs; [
            gdk-pixbuf
            glib
            gobject-introspection
            graphene
            gtk4
            harfbuzz
            libadwaita
            pango.out
          ]);
          giLibraryPath = pkgs.lib.makeLibraryPath (with pkgs; [
            cairo
            gdk-pixbuf
            glib
            graphene
            gtk4
            harfbuzz
            libadwaita
            pango
          ]);
        in
        {
          default = pkgs.stdenvNoCC.mkDerivation {
            pname = "nothing-ear-linux";
            version = "0.1.0";
            src = self;

            nativeBuildInputs = with pkgs; [
              makeWrapper
              wrapGAppsHook4
            ];

            buildInputs = with pkgs; [
              gtk4
              libadwaita
            ];

            dontBuild = true;
            dontWrapGApps = true;

            installPhase = ''
              runHook preInstall

              install -Dm755 nothing_ear_linux.py $out/share/nothing-ear-linux/nothing_ear_linux.py
              makeWrapper ${python}/bin/python3 $out/bin/nothing-ear-linux \
                --add-flags $out/share/nothing-ear-linux/nothing_ear_linux.py \
                --prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.bluez ]} \
                --prefix GI_TYPELIB_PATH : ${giTypelibPath} \
                --prefix LD_LIBRARY_PATH : ${giLibraryPath} \
                "''${gappsWrapperArgs[@]}"

              install -Dm644 ${./nothing-ear-linux.desktop} \
                $out/share/applications/nothing-ear-linux.desktop

              runHook postInstall
            '';

            meta = with pkgs.lib; {
              description = "Unofficial Linux companion app for Nothing Ear earbuds";
              homepage = "https://github.com/OPIYOdev/Nothing-Ear-Linux-";
              mainProgram = "nothing-ear-linux";
              platforms = platforms.linux;
            };
          };
        });

      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/nothing-ear-linux";
        };
      });

      formatter = forAllSystems (system: nixpkgs.legacyPackages.${system}.nixpkgs-fmt);
    };
}
