{
  description = "nixarchy.pixelbuds -- Pixel Buds battery and listening modes in the Omarchy bar";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAll = nixpkgs.lib.genAttrs systems;

      manifest = builtins.fromJSON (builtins.readFile ./manifest.json);

      pythonFor = pkgs: pkgs.python3.withPackages (p: [ p.pygobject3 ]);

      pluginFor =
        pkgs:
        let
          py = pythonFor pkgs;
        in
        # Plain copies: omarchy-plugin-validate refuses any symlink inside a
        # plugin folder. bridge/ stays a subdirectory because Service.qml
        # resolves bridge/pixelbuds_bridge.py relative to itself.
        pkgs.runCommand "nixarchy-pixelbuds-${manifest.version}"
          {
            meta = with pkgs.lib; {
              description = "Omarchy plugin: Pixel Buds battery and listening modes in the bar";
              homepage = "https://github.com/olafkfreund/nixarchy-pixelbuds";
              license = with licenses; [
                mit
                asl20
              ];
              platforms = platforms.linux;
            };
          }
          ''
            mkdir -p "$out/bridge"
            cp ${./manifest.json} "$out/manifest.json"
            cp ${./Model.js} "$out/Model.js"
            cp ${./Panel.qml} "$out/Panel.qml"
            cp ${./Service.qml} "$out/Service.qml"
            cp ${./LICENSE} "$out/LICENSE"
            cp ${./LICENSE-APACHE} "$out/LICENSE-APACHE"
            cp ${./NOTICE} "$out/NOTICE"
            cp ${./preview.png} "$out/preview.png"
            cp ${./bridge/pixelbuds_bridge.py} "$out/bridge/pixelbuds_bridge.py"
            cp ${./bridge/maestro.py} "$out/bridge/maestro.py"
            cp ${./bridge/casecache.py} "$out/bridge/casecache.py"
            chmod -R u+w "$out"

            # Upstream hardcodes Arch paths. Through envfs, /usr/bin/python3 on
            # NixOS has no PyGObject, and a cleared environment cannot find
            # omarchy-shell. --replace-fail so a moved literal fails the build.
            substituteInPlace "$out/Service.qml" \
              --replace-fail '"/usr/bin/python3"' '"${py}/bin/python3"' \
              --replace-fail '"/usr/bin/gdbus"' '"${pkgs.glib.bin}/bin/gdbus"'
            substituteInPlace "$out/Panel.qml" \
              --replace-fail '"/usr/bin/omarchy-shell"' '"/run/current-system/sw/bin/omarchy-shell"'
          '';
    in
    {
      packages = forAll (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        rec {
          default = nixarchy-pixelbuds;
          nixarchy-pixelbuds = pluginFor pkgs;
        }
      );

      checks = forAll (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          py = pythonFor pkgs;
          plugin = self.packages.${system}.default;
        in
        {
          default =
            pkgs.runCommand "nixarchy-pixelbuds-check"
              {
                nativeBuildInputs = [
                  py
                  pkgs.jq
                  pkgs.nodejs
                ];
              }
              ''
                # The offline suite, run against the packaged bridge. Only the
                # copied tests are rewritten to store paths. launch-boundary-test.sh
                # is skipped: it asserts the upstream /usr/bin literals that the
                # package replaces by design.
                cp -r ${./tests} tests
                cp -r ${plugin}/bridge bridge
                cp ${plugin}/Model.js Model.js
                chmod -R u+w .
                substituteInPlace tests/python/test_dbus_integration.py \
                  --replace-fail '"/usr/bin/dbus-daemon"' '"${pkgs.dbus}/bin/dbus-daemon"' \
                  --replace-fail '["/usr/bin/python3"' '["${py}/bin/python3"'
                (cd tests/python && PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s . -p 'test_*.py')
                node tests/js/model-test.js

                # No Arch binary path left in the shipped QML (an if: set -e ignores `! cmd`).
                if grep -n '"/usr/bin/\(python3\|gdbus\|omarchy-shell\)"' ${plugin}/*.qml; then
                  echo "Arch binary path left in the package" >&2; exit 1
                fi

                jq -e '.id == "nixarchy.pixelbuds"' ${plugin}/manifest.json > /dev/null
                for f in $(jq -r '.entryPoints[]' ${plugin}/manifest.json); do
                  test -f "${plugin}/$f" || { echo "entry point $f missing" >&2; exit 1; }
                done

                # omarchy-plugin-validate refuses symlinks inside a plugin.
                test -z "$(find ${plugin}/ -mindepth 1 -type l)"

                # The bridge runs with a cleared environment.
                env -i ${py}/bin/python3 -I -B -c 'import gi; gi.require_version("Gio", "2.0"); gi.require_version("GLib", "2.0"); from gi.repository import Gio, GLib'

                touch "$out"
              '';
        }
      );
    };
}
